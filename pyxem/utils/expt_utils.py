# -*- coding: utf-8 -*-
# Copyright 2017-2019 The pyXem developers
#
# This file is part of pyXem.
#
# pyXem is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyXem is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyXem.  If not, see <http://www.gnu.org/licenses/>.

import numpy as np
import scipy.ndimage as ndi
import pyxem as pxm  # for ElectronDiffraction2D

from scipy.ndimage.interpolation import shift
from scipy.interpolate import interp1d
from scipy.interpolate import RectBivariateSpline
from scipy.optimize import curve_fit, minimize
from skimage import transform as tf
from skimage import morphology, filters
from skimage.morphology import square, opening
from skimage.filters import (threshold_sauvola, threshold_otsu)
from skimage.draw import ellipse_perimeter
from skimage.feature import register_translation
from scipy.optimize import curve_fit
from tqdm import tqdm


"""
This module contains utility functions for processing electron diffraction
patterns.
"""


def _index_coords(z, origin=None):
    """Creates x & y coords for the indicies in a numpy array.

    Parameters
    ----------
    z : np.array()
        Two-dimensional data array containing signal.
    origin : tuple
        (x,y) defaults to the center of the image. Specify origin=(0,0) to set
        the origin to the *top-left* corner of the image.

    Returns
    -------
    x, y : arrays
        Corrdinates for the indices of a numpy array.
    """
    ny, nx = z.shape[:2]
    if origin is None:
        origin_x, origin_y = nx // 2, ny // 2
    else:
        origin_x, origin_y = origin

    x, y = np.meshgrid(np.arange(float(nx)), np.arange(float(ny)))

    x -= origin_x
    y -= origin_y
    return x, y


def _cart2polar(x, y):
    """Transform Cartesian coordinates to polar coordinates.

    Parameters
    ----------
    x, y : floats or arrays
        Cartesian coordinates

    Returns
    -------
    r, theta : floats or arrays
        Polar coordinates

    """
    r = np.sqrt(x**2 + y**2)
    theta = -np.arctan2(y, x)  # θ = 0 horizontal, +ve = anticlockwise
    return r, theta


def _polar2cart(r, theta):
    """Transform polar coordinates to Cartesian coordinates.

    Parameters
    ----------
    r, theta : floats or arrays
        Polar coordinates

    Returns
    -------
    x, y : floats or arrays
        Cartesian coordinates
    """
    # +ve quadrant in bottom right corner when plotted
    x = r * np.cos(theta)
    y = -r * np.sin(theta)
    return x, y


def radial_average(z, mask=None):
    """Calculate the radial profile by azimuthal averaging about the center.

    Parameters
    ----------
    z : np.array()
        Two-dimensional data array containing signal.
    mask : np.array()
        Array with the same dimensions as z comprizing 0s for excluded pixels
        and 1s for non-excluded pixels.

    Returns
    -------
    radial_profile : np.array()
        One-dimensional radial profile of z.
    """
    # geometric shape work, not 0 indexing
    center = ((z.shape[0] / 2) - 0.5, (z.shape[1] / 2) - 0.5)

    y, x = np.indices(z.shape)
    r = np.sqrt((x - center[1])**2 + (y - center[0])**2)
    r = np.rint(r - 0.5).astype(np.int)
    # the subtraction of 0.5 gets the 0 in the correct place

    if mask is None:
        tbin = np.bincount(r.ravel(), z.ravel())
        nr = np.bincount(r.ravel())
    else:
        # the mask is applied on the z array.
        masked_array = z * mask
        tbin = np.bincount(r.ravel(), masked_array.ravel())
        nr = np.bincount(r.ravel(), mask.ravel())

    averaged = np.nan_to_num(tbin / nr)

    return averaged


def reproject_polar(z, origin=None, jacobian=False, dr=1, dt=None):
    """
    Reprojects a 2D diffraction pattern into a polar coordinate system.

    Parameters
    ----------
    origin : tuple
        The coordinate (x0, y0) of the image center, relative to bottom-left. If
        'None'defaults to
    Jacobian : boolean
        Include ``r`` intensity scaling in the coordinate transform.
        This should be included to account for the changing pixel size that
        occurs during the transform.
    dr : float
        Radial coordinate spacing for the grid interpolation
        tests show that there is not much point in going below 0.5
    dt : float
        Angular coordinate spacing (in radians)
        if ``dt=None``, dt will be set such that the number of theta values
        is equal to the maximum value between the height or the width of
        the image.

    Returns
    -------
    output : 2D np.array
        The polar image (r, theta)

    Notes
    -----
    Adapted from: PyAbel, www.github.com/PyAbel/PyAbel

    """
    # bottom-left coordinate system requires numpy image to be np.flipud
    data = np.flipud(z)

    ny, nx = data.shape[:2]
    if origin is None:
        origin = (nx//2, ny//2)

    # Determine that the min and max r and theta coords will be...
    x, y = _index_coords(z, origin=origin)  # (x,y) coordinates of each pixel
    r, theta = _cart2polar(x, y)  # convert (x,y) -> (r,θ), note θ=0 is vertical

    nr = np.int(np.ceil((r.max()-r.min())/dr))

    if dt is None:
        nt = max(nx, ny)
    else:
        # dt in radians
        nt = np.int(np.ceil((theta.max()-theta.min())/dt))

    # Make a regular (in polar space) grid based on the min and max r & theta
    r_i = np.linspace(r.min(), r.max(), nr, endpoint=False)
    theta_i = np.linspace(theta.min(), theta.max(), nt, endpoint=False)
    theta_grid, r_grid = np.meshgrid(theta_i, r_i)

    # Project the r and theta grid back into pixel coordinates
    X, Y = _polar2cart(r_grid, theta_grid)

    X += origin[0]  # We need to shift the origin
    Y += origin[1]  # back to the bottom-left corner...
    xi, yi = X.flatten(), Y.flatten()
    coords = np.vstack((yi, xi))  # (map_coordinates requires a 2xn array)

    zi = ndi.map_coordinates(z, coords)
    output = zi.reshape((nr, nt))

    if jacobian:
        output = output*r_i[:, np.newaxis]

    return output


def ellipsoid_in_cartesian(r_list,
                           theta_list,
                           center,
                           axes_lengths=None,
                           angle=None):
    """Defines cartesian coordinates of points on an ellipsoid.

    Parameters
    ----------
    r_list: array
        list of all of the radius.  Can either be all values or even_spaced
    theta_list: array
        list of all of the radius.  Can either be all values or even_spaced
    center: array_like
        center of the ellipsoid
    lengths: float
        length of the major axis
    minor: float
        length of the minor axis
    angle: float
        angle of the major axis in radians

    Returns
    -------
    x_list: array_like
        list of x points
    y_list: array_like
        list of y points
    """
    # Averaging the major and minor axes
    if axes_lengths is not None:
        axes_avg = sum(axes_lengths)/2
        h_o = max(axes_lengths)/axes_avg  # major
        k_o = min(axes_lengths)/axes_avg
    else:
        h_o = 1
        k_o = 1
    r_mat = np.mat(r_list)

    # calculating points equally spaced annularly on a unit circle
    t_sin = np.mat([np.sin(t)for t in theta_list])
    t_cos = np.mat([np.cos(t)for t in theta_list])
    # unit circle to ellipses at r spacing
    x_circle = r_mat.transpose()*t_sin*h_o
    y_circle = r_mat.transpose()*t_cos * k_o

    if angle is not None:
        # angle of rotation
        cos_angle = np.cos(angle)
        sin_angle = np.sin(angle)
        x_list = x_circle*cos_angle - y_circle*sin_angle
        x_list = np.add(x_list, center[0])
        y_list = y_circle*cos_angle + x_circle*sin_angle
        y_list = np.add(y_list, center[1])
        return np.array(x_list), np.array(y_list)
    else:
        x_list = np.add(x_circle, center[0])
        y_list = np.add(y_circle, center[1])
    return np.array(x_list), np.array(y_list)


def reproject_cartesian_to_polar(img,
                                 center=None,
                                 angle=None,
                                 lengths=None,
                                 radius=[0,100],
                                 phase_width=720):
    """Project an image from cartesian coordinates to polar coordinates.

    Parameters
    ----------

    img:array-like
        A n by 2-d array for the image to convert to polar coordinates
    center: list
        [X,Y] coordinates for the center of the image
    angle: float
        Angle of rotation if the sample is elliptical
    lengths: list
        The major and minor lengths of the ellipse
    radius: list
        The inner and outer indexes to define the radius by.
    phase_width: int
        The number of "pixels" in the polar image along the x direction

    Returns
    -------
    polar_img: array-like
        A numpy array of the input img  in polar coordiates.
        Dim (radius[1]-radius[0]) x phase_width
    """
    img_shape = np.shape(img)
    initial_y, initial_x = range(0, img_shape[-2]), range(0, img_shape[-1])
    if center is None:
        center = np.true_divide(img_shape[-2:], 2)
    final_the = np.linspace(0, 2*np.pi, num=phase_width)
    final_rad = np.arange(radius[0], radius[1], 1)
    final_x, final_y = ellipsoid_in_cartesian(final_rad,
                                              final_the,
                                              center,
                                              axes_lengths=lengths,
                                              angle=angle)
    intensity = img.data

    # setting masked values to negative values. Anything interpolated from
    # masked values becomes negative
    try:
        intensity[img.mask] = -999999
    except AttributeError:
        pass
    spline = RectBivariateSpline(initial_x, initial_y, intensity, kx=1, ky=1)  # bi-linear spline (Takes 90% of time)
    polar_img = np.array(spline.ev(final_x, final_y))
    polar_img = np.reshape(polar_img, (int(radius[1]-radius[0]), phase_width))

    # outputting new mask
    polar_img[polar_img < 0] = -10

    return polar_img


def gain_normalise(z, dref, bref):
    """Apply gain normalization to experimentally acquired electron
    diffraction pattern.

    Parameters
    ----------
    z : np.array()
        Two-dimensional data array containing signal.
    dref : ElectronDiffraction2D
        Two-dimensional data array containing dark reference.
    bref : ElectronDiffraction2D
        Two-dimensional data array containing bright reference.

    Returns
    -------
    z1 : np.array()
        Two dimensional data array of gain normalized z.
    """
    return ((z - dref) / (bref - dref)) * np.mean((bref - dref))


def remove_dead(z, deadpixels, deadvalue="average", d=1):
    """Remove dead pixels from experimental electron diffraction patterns.

    Parameters
    ----------
    z : np.array()
        Two-dimensional data array containing signal.
    deadpixels : np.array()
        Array containing the array indices of dead pixels in the diffraction
        pattern.
    deadvalue : string
        Specify how deadpixels should be treated, options are;
            'average': takes the average of adjacent pixels
            'nan':  sets the dead pixel to nan

    Returns
    -------
    img : array
        Two-dimensional data array containing z with dead pixels removed.
    """
    z_bar = np.copy(z)
    if deadvalue == 'average':
        for (i, j) in deadpixels:
            neighbours = z[i - d:i + d + 1, j - d:j + d + 1].flatten()
            z_bar[i, j] = np.mean(neighbours)

    elif deadvalue == 'nan':
        for (i, j) in deadpixels:
            z_bar[i, j] = np.nan
    else:
        raise NotImplementedError("The method specified is not implemented. "
                                  "See documentation for available "
                                  "implementations.")

    return z_bar


def convert_affine_to_transform(D, shape):
    """ Converts an affine transform on a diffraction pattern to a suitable
    form for skimage.transform.warp()

    Parameters
    ----------
    D : np.array
        Affine transform to be applied
    shape : tuple
        Shape tuple in form (y,x) for the diffraction pattern

    Returns
    -------
    transformation : np.array
        3x3 numpy array of the transformation to be applied.

    """

    shift_x = (shape[1] - 1) / 2
    shift_y = (shape[0] - 1) / 2

    tf_shift = tf.SimilarityTransform(translation=[-shift_x, -shift_y])
    tf_shift_inv = tf.SimilarityTransform(translation=[shift_x, shift_y])

    # This defines the transform you want to perform
    distortion = tf.AffineTransform(matrix=D)

    # skimage transforms can be added like this, does matrix multiplication,
    # hence the need for the brackets. (Note tf.warp takes the inverse)
    transformation = (tf_shift + (distortion + tf_shift_inv)).inverse

    return transformation


def apply_transformation(z, transformation, keep_dtype, order=1, *args, **kwargs):
    """Apply a transformation to a 2-dimensional array.

    Parameters
    ----------
    z : np.array
        Array to be transformed
    transformation : np.array
        3x3 numpy array specifying the transformation to be applied.
    order : int
        Interpolation order.
    keep_dtype : bool
        If True dtype of returned object is that of z
    *args :
        To be passed to skimage.warp
    **kwargs :
        To be passed to skimage.warp

    Returns
    -------
    trans : array
        Affine transformed diffraction pattern.

    Notes
    -----
    Generally used in combination with pyxem.expt_utils.convert_affine_to_transform
    """
    if keep_dtype == False:
        trans = tf.warp(z, transformation,
                        order=order, *args, **kwargs)
    if keep_dtype == True:
        trans = tf.warp(z, transformation,
                        order=order, preserve_range=True, *args, **kwargs)
        trans = trans.astype(z.dtype)

    return trans


def regional_filter(z, h):
    """Perform a h-dome regional filtering of the an image for background
    subtraction.

    Parameters
    ----------
    h : float
        h-dome cutoff value.

    Returns
    -------
        h-dome subtracted image as np.array
    """
    seed = np.copy(z)
    seed = z - h
    mask = z
    dilated = morphology.reconstruction(seed, mask, method='dilation')

    return z - dilated


def subtract_background_dog(z, sigma_min, sigma_max):
    """Difference of gaussians method for background removal.

    Parameters
    ----------
    sigma_max : float
        Large gaussian blur sigma.
    sigma_min : float
        Small gaussian blur sigma.

    Returns
    -------
        Denoised diffraction pattern as np.array
    """
    blur_max = ndi.gaussian_filter(z, sigma_max)
    blur_min = ndi.gaussian_filter(z, sigma_min)

    return np.maximum(np.where(blur_min > blur_max, z, 0) - blur_max, 0)


def subtract_background_median(z, footprint=19, implementation='scipy'):
    """Remove background using a median filter.

    Parameters
    ----------
    footprint : int
        size of the window that is convoluted with the array to determine
        the median. Should be large enough that it is about 3x as big as the
        size of the peaks.
    implementation: str
        One of 'scipy', 'skimage'. Skimage is much faster, but it messes with
        the data format. The scipy implementation is safer, but slower.

    Returns
    -------
        Pattern with background subtracted as np.array
    """

    if implementation == 'scipy':
        bg_subtracted = z - ndi.median_filter(z, size=footprint)
    elif implementation == 'skimage':
        selem = morphology.square(footprint)
        # skimage only accepts input image as uint16
        bg_subtracted = z - filters.median(z.astype(np.uint16), selem).astype(z.dtype)
    else:
        raise ValueError("Unknown implementation `{}`".format(implementation))

    return np.maximum(bg_subtracted, 0)


def subtract_reference(z, bg):
    """Subtracts background using a user-defined background pattern.

    Parameters
    ----------
    z : np.array()
        Two-dimensional data array containing signal.
    bg: array()
        User-defined diffraction pattern to be subtracted as background.

    Returns
    -------
    im : np.array()
        Two-dimensional data array containing signal with background removed.
    """
    im = z.astype(np.float64) - bg
    for i in range(0, z.shape[0]):
        for j in range(0, z.shape[1]):
            if im[i, j] < 0:
                im[i, j] = 0
    return im


def circular_mask(shape, radius, center=None):
    """Produces a mask of radius 'r' centered on 'center' of shape 'shape'.

    Parameters
    ----------
    shape : tuple
        The shape of the signal to be masked.
    radius : int
        The radius of the circular mask.
    center : tuple (optional)
        The center of the circular mask. Default: (0, 0)

    Returns
    -------
    mask : np.array()
        The circular mask.

    """
    l_x, l_y = shape
    x, y = center if center else (l_x / 2, l_y / 2)
    X, Y = np.ogrid[:l_x, :l_y]
    mask = (X - x) ** 2 + (Y - y) ** 2 < radius ** 2
    return mask


def reference_circle(coords, dimX, dimY, radius):
    """Draw the perimeter of an circle at a given position in the diffraction
    pattern (e.g. to provide a reference for finding the direct beam center).

    Parameters
    ----------
    coords : np.array size n,2
        size n,2 array of coordinates to draw the circle.
    dimX : int
        first dimension of the diffraction pattern (size)
    dimY : int
        second dimension of the diffraction pattern (size)
    radius : int
        radius of the circle to be drawn

    Returns
    -------
    img: np.array
        Array containing the circle at the position given in the coordinates.
    """
    img = np.zeros((dimX, dimY))

    for n in range(np.size(coords, 0)):
        rr, cc = ellipse_perimeter(coords[n, 0], coords[n, 1], radius, radius)
        img[rr, cc] = 1

    return img


def _find_peak_max(arr: np.ndarray, sigma: int, upsample_factor: int = 50, window: int = 10, kind: int = 3) -> float:
    """Find the index of the pixel corresponding to peak maximum in 1D pattern

    Parameters
    ----------
    sigma : int
        Sigma value for Gaussian blurring kernel for initial beam center estimation.
    upsample_factor : int
        Upsample factor for subpixel maximum finding, i.e. the maximum will
        be found with a precision of 1 / upsample_factor of a pixel.
    kind : str or int, optional
        Specifies the kind of interpolation as a string (‘linear’, ‘nearest’,
        ‘zero’, ‘slinear’, ‘quadratic’, ‘cubic’, ‘previous’, ‘next’, where
        ‘zero’, ‘slinear’, ‘quadratic’ and ‘cubic’ refer to a spline
        interpolation of zeroth, first, second or third order; ‘previous’
        and ‘next’ simply return the previous or next value of the point) or as
        an integer specifying the order of the spline interpolator to use.
    window : int
       A box of size 2*window+1 around the first estimate is taken and
       expanded by `upsample_factor` to to interpolate the pattern to get
       the peak maximum position with subpixel precision.

    Returns
    -------
    center: float
        Pixel position of the maximum
    """
    y1 = ndi.filters.gaussian_filter1d(arr, sigma)
    c1 = np.argmax(y1)  # initial guess for beam center

    m = upsample_factor
    w = window
    win_len = 2 * w + 1

    try:
        r1 = np.linspace(c1 - w, c1 + w, win_len)
        f = interp1d(r1, y1[c1 - w: c1 + w + 1], kind=kind)
        r2 = np.linspace(c1 - w, c1 + w, win_len * m)  # extrapolate for subpixel accuracy
        y2 = f(r2)
        c2 = np.argmax(y2) / m  # find beam center with `m` precision
    except ValueError:  # if c1 is too close to the edges, return initial guess
        center = c1
    else:
        center = c2 + c1 - w

    return center


def find_beam_center_interpolate(img: np.ndarray, sigma: int = 30, upsample_factor: int = 100, kind: int = 3) -> (float, float):
    """Find the center of the primary beam in the image `img` by summing along
    X/Y directions and finding the position along the two directions independently.

    Parameters
    ----------
    sigma : int
        Sigma value for Gaussian blurring kernel for initial beam center estimation.
    upsample_factor : int
        Upsample factor for subpixel beam center finding, i.e. the center will
        be found with a precision of 1 / upsample_factor of a pixel.
    kind : str or int, optional
        Specifies the kind of interpolation as a string (‘linear’, ‘nearest’,
        ‘zero’, ‘slinear’, ‘quadratic’, ‘cubic’, ‘previous’, ‘next’, where
        ‘zero’, ‘slinear’, ‘quadratic’ and ‘cubic’ refer to a spline
        interpolation of zeroth, first, second or third order; ‘previous’
        and ‘next’ simply return the previous or next value of the point) or as
        an integer specifying the order of the spline interpolator to use.

    Returns
    -------
    center : np.array
        np.array containing indices of estimated direct beam positon.
    """
    xx = np.sum(img, axis=1)
    yy = np.sum(img, axis=0)

    cx = _find_peak_max(xx, sigma, upsample_factor=upsample_factor, kind=kind)
    cy = _find_peak_max(yy, sigma, upsample_factor=upsample_factor, kind=kind)

    center = np.array([cx, cy])
    return center


def find_beam_center_blur(z: np.ndarray, sigma: int = 30) -> np.ndarray:
    """Estimate direct beam position by blurring the image with a large
    Gaussian kernel and finding the maximum.

    Parameters
    ----------
    sigma : float
        Sigma value for Gaussian blurring kernel.

    Returns
    -------
    center : np.array
        np.array containing indices of estimated direct beam positon.
    """
    blurred = ndi.gaussian_filter(z, sigma, mode='wrap')
    center = np.unravel_index(blurred.argmax(), blurred.shape)
    return np.array(center)


def find_beam_offset_cross_correlation(z, radius_start=4, radius_finish=8):
    """Find the offset of the direct beam from the image center by a cross-correlation algorithm.
    The shift is calculated relative to an circle perimeter. The circle can be
    refined across a range of radii during the centring procedure to improve
    performance in regions where the direct beam size changes,
    e.g. during sample thickness variation.

    Parameters
    ----------
    radius_start : int
        The lower bound for the radius of the central disc to be used in the
        alignment.
    radius_finish : int
        The upper bounds for the radius of the central disc to be used in the
        alignment.

    Returns
    -------
    shift: np.array
        np.array containing offset (from center) of the direct beam positon.
    """
    radiusList = np.arange(radius_start, radius_finish)
    errRecord = np.zeros_like(radiusList, dtype='single')
    origin = np.array([[round(np.size(z, axis=-2) / 2), round(np.size(z, axis=-1) / 2)]])

    for ind in np.arange(0, np.size(radiusList)):
        radius = radiusList[ind]
        ref = reference_circle(origin, np.size(z, axis=-2), np.size(z, axis=-1), radius)
        h0 = np.hanning(np.size(ref, 0))
        h1 = np.hanning(np.size(ref, 1))
        hann2d = np.sqrt(np.outer(h0, h1))
        ref = hann2d * ref
        im = hann2d * z
        shift, error, diffphase = register_translation(ref, im, 10)
        errRecord[ind] = error
        index_min = np.argmin(errRecord)

    ref = reference_circle(origin, np.size(z, axis=-2), np.size(z, axis=-1), radiusList[index_min])
    h0 = np.hanning(np.size(ref, 0))
    h1 = np.hanning(np.size(ref, 1))
    hann2d = np.sqrt(np.outer(h0, h1))
    ref = hann2d * ref
    im = hann2d * z
    shift, error, diffphase = register_translation(ref, im, 100)

    return (shift - 0.5)

def peaks_as_gvectors(z, center, calibration):
    """Converts peaks found as array indices to calibrated units, for use in a
    hyperspy map function.

    Parameters
    ----------
    z : numpy array
        peak postitions as array indices.
    center : numpy array
        diffraction pattern center in array indices.
    calibration : float
        calibration in reciprocal Angstroms per pixels.

    Returns
    -------
    g : numpy array
        peak positions in calibrated units.

    """
    g = (z - center) * calibration
    return np.array([g[0].T[1], g[0].T[0]]).T


def investigate_dog_background_removal_interactive(sample_dp,
                                                   std_dev_maxs,
                                                   std_dev_mins):
    """Utility function to help the parameter selection for the difference of
    gaussians (dog) background subtraction method

    Parameters
    ----------
    sample_dp : ElectronDiffraction2D
        A single diffraction pattern
    std_dev_maxs : iterable
        Linearly spaced maximum standard deviations to be tried, ascending
    std_dev_mins : iterable
        Linearly spaced minimum standard deviations to be tried, ascending

    Returns
    -------
    A hyperspy like navigation (sigma parameters), signal (proccessed patterns)
    plot

    See Also
    --------
    subtract_background_dog : The background subtraction method used.
    np.arange : Produces suitable objects for std_dev_maxs

    """
    gauss_processed = np.empty((
        len(std_dev_maxs),
        len(std_dev_mins),
        *sample_dp.axes_manager.signal_shape))

    for i, std_dev_max in enumerate(tqdm(std_dev_maxs, leave=False)):
        for j, std_dev_min in enumerate(std_dev_mins):
            gauss_processed[i, j] = sample_dp.remove_background('gaussian_difference',
                                                                sigma_min=std_dev_min, sigma_max=std_dev_max,
                                                                show_progressbar=False)
    dp_gaussian = pxm.ElectronDiffraction2D(gauss_processed)
    dp_gaussian.metadata.General.title = 'Gaussian preprocessed'
    dp_gaussian.axes_manager.navigation_axes[0].name = r'$\sigma_{\mathrm{min}}$'
    dp_gaussian.axes_manager.navigation_axes[1].name = r'$\sigma_{\mathrm{max}}$'
    for axes_number, axes_value_list in [(0, std_dev_mins), (1, std_dev_maxs)]:
        dp_gaussian.axes_manager.navigation_axes[axes_number].offset = axes_value_list[0]
        dp_gaussian.axes_manager.navigation_axes[axes_number].scale = axes_value_list[1] - axes_value_list[0]
        dp_gaussian.axes_manager.navigation_axes[axes_number].units = ''

    dp_gaussian.plot(cmap='viridis')
    return None
