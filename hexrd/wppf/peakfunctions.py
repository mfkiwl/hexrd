# ============================================================
# Copyright (c) 2012, Lawrence Livermore National Security, LLC.
# Produced at the Lawrence Livermore National Laboratory.
# Written by Joel Bernier <bernier2@llnl.gov> and others.
# LLNL-CODE-529294.
# All rights reserved.
#
# This file is part of HEXRD. For details on dowloading the source,
# see the file COPYING.
#
# Please also see the file LICENSE.
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License (as published by the Free
# Software Foundation) version 2.1 dated February 1999.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the IMPLIED WARRANTY OF MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the terms and conditions of the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program (see file LICENSE); if not, write to
# the Free Software Foundation, Inc., 59 Temple Place, Suite 330,
# Boston, MA 02111-1307 USA or visit <http://www.gnu.org/licenses/>.
# ============================================================

import numpy as np
import copy
from hexrd import constants
from scipy.special import exp1, erfc
from hexrd.utils.decorators import numba_njit_if_available

if constants.USE_NUMBA:
    from numba import prange
else:
    prange = range

# addr = get_cython_function_address("scipy.special.cython_special", "exp1")
# functype = ctypes.CFUNCTYPE(ctypes.c_double, ctypes.c_double)
# exp1_fn = functype(addr)

gauss_width_fact = constants.sigma_to_fwhm
lorentz_width_fact = 2.

# FIXME: we need this for the time being to be able to parse multipeak fitting
# results; need to wrap all this up in a class in the future!
mpeak_nparams_dict = {
    'gaussian': 3,
    'lorentzian': 3,
    'pvoigt': 4,
    'split_pvoigt': 6
}

"""
Calgliotti and Lorentzian FWHM functions
"""
@numba_njit_if_available(cache=True, nogil=True)
def _gaussian_fwhm(uvw,
                   P,
                   gamma_ani_sqr,
                   eta_mixing,
                   tth,
                   dsp):
    """
    @AUTHOR:     Saransh Singh, Lawrence Livermore National Lab, saransh1@llnl.gov
    @DATE:       05/20/2020 SS 1.0 original
    @DETAILS:    calculates the fwhm of gaussian component
                 uvw cagliotti parameters
                 P Scherrer broadening (generally not refined)
                 gamma_ani_sqr anisotropic broadening term
                 eta_mixing mixing factor
                 tth two theta in degrees
                 dsp d-spacing
    """
    U, V, W = uvw
    th = np.radians(0.5*tth)
    tanth = np.tan(th)
    cth2 = np.cos(th)**2.0
    sig2_ani = gamma_ani_sqr*(1.-eta_mixing)**2*dsp**4
    sigsqr = (U+sig2_ani) * tanth**2 + V * tanth + W + P/cth2
    if(sigsqr <= 0.):
        sigsqr = 1.0e-6

    return np.sqrt(sigsqr)*1e-2


@numba_njit_if_available(cache=True, nogil=True)
def _lorentzian_fwhm(xy,
                     xy_sf,
                     gamma_ani_sqr,
                     eta_mixing,
                     tth,
                     dsp,
                     strain_direction_dot_product,
                     is_in_sublattice):
    """
    @AUTHOR:     Saransh Singh, Lawrence Livermore National Lab, saransh1@llnl.gov
    @DATE:       07/20/2020 SS 1.0 original
    @DETAILS:    calculates the size and strain broadening for Lorentzian peak
                xy [X, Y] lorentzian broadening
                xy_sf stacking fault dependent peak broadening
                gamma_ani_sqr anisotropic broadening term
                eta_mixing mixing factor
                tth two theta in degrees
                dsp d-spacing
                is_in_sublattice if hkl in sublattice, then extra broadening
                else regular broadening
    """
    X, Y = xy
    Xe, Ye, Xs = xy_sf
    th = np.radians(0.5*tth)
    tanth = np.tan(th)
    cth = np.cos(th)
    sig_ani = np.sqrt(gamma_ani_sqr)*eta_mixing*dsp**2
    if is_in_sublattice:
        xx = Xe*strain_direction_dot_product
    else:
        xx = Xs*strain_direction_dot_product
    gamma = (X+xx)/cth + (Y+Ye+sig_ani)*tanth
    return gamma*1e-2

@numba_njit_if_available(cache=True, nogil=True)
def _anisotropic_peak_broadening(shkl, hkl):
    """
    this function generates the broadening as
    a result of anisotropic broadening. details in
    P.Stephens, J. Appl. Cryst. (1999). 32, 281-289
    a total of 15 terms, some of them zero. in this
    function, we will just use all the terms. it is
    assumed that the user passes on the correct values
    for shkl with appropriate zero values
    The order is the same as the wppfsupport._shkl_name
    variable
    ["s400", "s040", "s004", "s220", "s202", "s022",
              "s310", "s103", "s031", "s130", "s301", "s013",
              "s211", "s121", "s112"]
    """
    h,k,l = hkl
    gamma_sqr = (shkl[0]*h**4 +
                shkl[1]*k**4 +
                shkl[2]*l**4 +
                3.0*(shkl[3]*(h*k)**2 +
                     shkl[4]*(h*l)**2 +
                     shkl[5]*(k*l)**2)+
                2.0*(shkl[6]*k*h**3 +
                     shkl[7]*h*l**3 +
                     shkl[8]*l*k**3 +
                     shkl[9]*h*k**3 +
                     shkl[10]*l*h**3 +
                     shkl[11]*k*l**3) +
                4.0*(shkl[12]*k*l*h**2 +
                     shkl[13]*h*l*k**2 +
                     shkl[14]*h*k*l**2))

    return gamma_sqr

def _anisotropic_gaussian_component(gamma_sqr, eta_mixing):
    """
    gaussian component in anisotropic broadening
    """
    return gamma_sqr*(1. - eta_mixing)**2

def _anisotropic_lorentzian_component(gamma_sqr, eta_mixing):
    """
    lorentzian component in anisotropic broadening
    """
    return np.sqrt(gamma_sqr)*eta_mixing

# =============================================================================
# 1-D Gaussian Functions
# =============================================================================
# Split the unit gaussian so this can be called for 2d and 3d functions


@numba_njit_if_available(cache=True, nogil=True)
def _unit_gaussian(p, x):
    """
    Required Arguments:
    p -- (m) [x0,FWHM]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    x0 = p[0]
    FWHM = p[1]
    sigma = FWHM/gauss_width_fact

    f = np.exp(-(x-x0)**2/(2.*sigma**2.))
    return f


def _gaussian1d_no_bg(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    A = p[0]
    f = A*_unit_gaussian(p[[1, 2]], x)
    return f


def gaussian1d(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM,c0,c1]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    bg0 = p[3]
    bg1 = p[4]

    f = _gaussian1d_no_bg(p[:3], x)+bg0+bg1*x

    return f


def _gaussian1d_no_bg_deriv(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM]
    x -- (n) ndarray of coordinate positions

    Outputs:
    d_mat -- (3 x n) ndarray of derivative values at positions x
    """

    x0 = p[1]
    FWHM = p[2]

    sigma = FWHM/gauss_width_fact

    dydx0 = _gaussian1d_no_bg(p, x)*((x-x0)/(sigma**2.))
    dydA = _unit_gaussian(p[[1, 2]], x)
    dydFWHM = _gaussian1d_no_bg(p, x)*((x-x0)**2./(sigma**3.))/gauss_width_fact

    d_mat = np.zeros((len(p), len(x)))

    d_mat[0, :] = dydA
    d_mat[1, :] = dydx0
    d_mat[2, :] = dydFWHM

    return d_mat


def gaussian1d_deriv(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM,c0,c1]
    x -- (n) ndarray of coordinate positions

    Outputs:
    d_mat -- (5 x n) ndarray of derivative values at positions x
    """

    d_mat = np.zeros((len(p), len(x)))
    d_mat[0:3, :] = _gaussian1d_no_bg_deriv(p[0:3], x)
    d_mat[3, :] = 1.
    d_mat[4, :] = x

    return d_mat


# =============================================================================
# 1-D Lorentzian Functions
# =============================================================================
# Split the unit function so this can be called for 2d and 3d functions
@numba_njit_if_available(cache=True, nogil=True)
def _unit_lorentzian(p, x):
    """
    Required Arguments:
    p -- (m) [x0,FWHM]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    x0 = p[0]
    FWHM = p[1]
    gamma = FWHM/lorentz_width_fact

    f = gamma / ((x-x0)**2 + gamma**2)
    return f


def _lorentzian1d_no_bg(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    A = p[0]
    f = A*_unit_lorentzian(p[[1, 2]], x)

    return f


def lorentzian1d(p, x):
    """
    Required Arguments:
    p -- (m) [x0,FWHM,c0,c1]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    bg0 = p[3]
    bg1 = p[4]

    f = _lorentzian1d_no_bg(p[:3], x)+bg0+bg1*x

    return f


def _lorentzian1d_no_bg_deriv(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM]
    x -- (n) ndarray of coordinate positions

    Outputs:
    d_mat -- (3 x n) ndarray of derivative values at positions x
    """

    x0 = p[1]
    FWHM = p[2]

    gamma = FWHM/lorentz_width_fact

    dydx0 = _lorentzian1d_no_bg(p, x)*((2.*(x-x0))/((x-x0)**2 + gamma**2))
    dydA = _unit_lorentzian(p[[1, 2]], x)
    dydFWHM = _lorentzian1d_no_bg(p, x) \
        * ((2.*(x-x0)**2.)/(gamma*((x-x0)**2 + gamma**2)))/lorentz_width_fact

    d_mat = np.zeros((len(p), len(x)))
    d_mat[0, :] = dydA
    d_mat[1, :] = dydx0
    d_mat[2, :] = dydFWHM

    return d_mat


def lorentzian1d_deriv(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM,c0,c1]
    x -- (n) ndarray of coordinate positions

    Outputs:
    d_mat -- (5 x n) ndarray of derivative values at positions x
    """

    d_mat = np.zeros((len(p), len(x)))
    d_mat[0:3, :] = _lorentzian1d_no_bg_deriv(p[0:3], x)
    d_mat[3, :] = 1.
    d_mat[4, :] = x

    return d_mat


# =============================================================================
# 1-D Psuedo Voigt Functions
# =============================================================================
# Split the unit function so this can be called for 2d and 3d functions
def _unit_pvoigt1d(p, x):
    """
    Required Arguments:
    p -- (m) [x0,FWHM,n]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    n = p[2]

    f = (n*_unit_gaussian(p[:2], x)+(1.-n)*_unit_lorentzian(p[:2], x))
    return f


def _pvoigt1d_no_bg(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM,n]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    A = p[0]
    f = A*_unit_pvoigt1d(p[[1, 2, 3]], x)
    return f


def pvoigt1d(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM,n,c0,c1]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    bg0 = p[4]
    bg1 = p[5]

    f = _pvoigt1d_no_bg(p[:4], x)+bg0+bg1*x

    return f


# =============================================================================
# 1-D Split Psuedo Voigt Functions
# =============================================================================

def _split_pvoigt1d_no_bg(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM-,FWHM+,n-,n+]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    A = p[0]
    x0 = p[1]

    f = np.zeros(x.shape[0])

    # Define halves, using gthanorequal and lthan, choice is arbitrary
    xr = x >= x0
    xl = x < x0

    # +
    right = np.where(xr)[0]

    f[right] = A*_unit_pvoigt1d(p[[1, 3, 5]], x[right])

    # -
    left = np.where(xl)[0]
    f[left] = A*_unit_pvoigt1d(p[[1, 2, 4]], x[left])

    return f


def split_pvoigt1d(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,FWHM-,FWHM+,n-,n+,c0,c1]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    bg0 = p[6]
    bg1 = p[7]

    f = _split_pvoigt1d_no_bg(p[:6], x)+bg0+bg1*x

    return f


# =============================================================================
# Tanh Step Down
# =============================================================================

def tanh_stepdown_nobg(p, x):
    """
    Required Arguments:
    p -- (m) [A,x0,w]
    x -- (n) ndarray of coordinate positions

    Outputs:
    f -- (n) ndarray of function values at positions x
    """

    A = p[0]
    x0 = p[1]
    w = p[2]

    f = A*(0.5*(1.-np.tanh((x-x0)/w)))

    return f


# =============================================================================
# 2-D Rotation Coordinate Transform
# =============================================================================

def _2d_coord_transform(theta, x0, y0, x, y):
    xprime = np.cos(theta)*x+np.sin(theta)*y
    yprime = -np.sin(theta)*x+np.cos(theta)*y

    x0prime = np.cos(theta)*x0+np.sin(theta)*y0
    y0prime = -np.sin(theta)*x0+np.cos(theta)*y0

    return x0prime, y0prime, xprime, yprime


# =============================================================================
# 2-D Gaussian Function
# =============================================================================

def _gaussian2d_no_bg(p, x, y):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,FWHMx,FWHMy]
    x -- (n x o) ndarray of coordinate positions for dimension 1
    y -- (n x o) ndarray of coordinate positions for dimension 1

    Outputs:
    f -- (n x 0) ndarray of function values at positions (x,y)
    """

    A = p[0]
    f = A*_unit_gaussian(p[[1, 3]], x)*_unit_gaussian(p[[2, 4]], y)
    return f


def _gaussian2d_rot_no_bg(p, x, y):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,FWHMx,FWHMy,theta]
    x -- (n x o) ndarray of coordinate positions for dimension 1
    y -- (n x o) ndarray of coordinate positions for dimension 2

    Outputs:
    f -- (n x o) ndarray of function values at positions (x,y)
    """

    theta = p[5]

    x0prime, y0prime, xprime, yprime = _2d_coord_transform(
        theta, p[1], p[2], x, y)

    # this copy was needed so original parameters set isn't changed
    newp = copy.copy(p)

    newp[1] = x0prime
    newp[2] = y0prime

    f = _gaussian2d_no_bg(newp[:5], xprime, yprime)

    return f


def gaussian2d_rot(p, x, y):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,FWHMx,FWHMy,theta,c0,c1x,c1y]
    x -- (n x o) ndarray of coordinate positions for dimension 1
    y -- (n x o) ndarray of coordinate positions for dimension 2

    Outputs:
    f -- (n x o) ndarray of function values at positions (x,y)
    """

    bg0 = p[6]
    bg1x = p[7]
    bg1y = p[8]

    f = _gaussian2d_rot_no_bg(p[:6], x, y)+(bg0+bg1x*x+bg1y*y)
    return f


def gaussian2d(p, x, y):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,FWHMx,FWHMy,c0,c1x,c1y]
    x -- (n x o) ndarray of coordinate positions for dimension 1
    y -- (n x o) ndarray of coordinate positions for dimension 2

    Outputs:
    f -- (n x o) ndarray of function values at positions (x,y)
    """

    bg0 = p[5]
    bg1x = p[6]
    bg1y = p[7]

    f = _gaussian2d_no_bg(p[:5], x, y)+(bg0+bg1x*x+bg1y*y)
    return f


# =============================================================================
# 2-D Split Psuedo Voigt Function
# =============================================================================

def _split_pvoigt2d_no_bg(p, x, y):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,FWHMx-,FWHMx+,FWHMy-,FWHMy+,nx-,nx+,ny-,ny+]
    x -- (n x o) ndarray of coordinate positions for dimension 1
    y -- (n x o) ndarray of coordinate positions for dimension 2

    Outputs:
    f -- (n x o) ndarray of function values at positions (x,y)
    """

    A = p[0]
    x0 = p[1]
    y0 = p[2]

    f = np.zeros([x.shape[0], x.shape[1]])

    # Define quadrants, using gthanorequal and lthan, choice is arbitrary
    xr = x >= x0
    xl = x < x0
    yr = y >= y0
    yl = y < y0

    # ++
    q1 = np.where(xr & yr)
    f[q1] = A*_unit_pvoigt1d(p[[1, 4, 8]], x[q1]) * \
        _unit_pvoigt1d(p[[2, 6, 10]], y[q1])

    # +-
    q2 = np.where(xr & yl)
    f[q2] = A*_unit_pvoigt1d(p[[1, 4, 8]], x[q2]) * \
        _unit_pvoigt1d(p[[2, 5, 9]], y[q2])

    # -+
    q3 = np.where(xl & yr)
    f[q3] = A*_unit_pvoigt1d(p[[1, 3, 7]], x[q3]) * \
        _unit_pvoigt1d(p[[2, 6, 10]], y[q3])

    # --
    q4 = np.where(xl & yl)
    f[q4] = A*_unit_pvoigt1d(p[[1, 3, 7]], x[q4]) * \
        _unit_pvoigt1d(p[[2, 5, 9]], y[q4])

    return f


def _split_pvoigt2d_rot_no_bg(p, x, y):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,FWHMx-,FWHMx+,FWHMy-,FWHMy+,nx-,nx+,ny-,ny+,theta]
    x -- (n x o) ndarray of coordinate positions for dimension 1
    y -- (n x o) ndarray of coordinate positions for dimension 2

    Outputs:
    f -- (n x o) ndarray of function values at positions (x,y)
    """

    theta = p[11]

    x0prime, y0prime, xprime, yprime = _2d_coord_transform(
        theta, p[1], p[2], x, y)

    # this copy was needed so original parameters set isn't changed
    newp = copy.copy(p)

    newp[1] = x0prime
    newp[2] = y0prime

    f = _split_pvoigt2d_no_bg(newp[:11], xprime, yprime)

    return f


def split_pvoigt2d_rot(p, x, y):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,FWHMx-,FWHMx+,FWHMy-,FWHMy+,
              nx-,nx+,ny-,ny+,theta,c0,c1x,c1y]
    x -- (n x o) ndarray of coordinate positions for dimension 1
    y -- (n x o) ndarray of coordinate positions for dimension 2

    Outputs:
    f -- (n x o) ndarray of function values at positions (x,y)
    """

    bg0 = p[12]
    bg1x = p[13]
    bg1y = p[14]

    f = _split_pvoigt2d_rot_no_bg(p[:12], x, y)+(bg0+bg1x*x+bg1y*y)

    return f


# =============================================================================
# 3-D Gaussian Function
# =============================================================================

def _gaussian3d_no_bg(p, x, y, z):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,z0,FWHMx,FWHMy,FWHMz]
    x -- (n x o x q) ndarray of coordinate positions for dimension 1
    y -- (n x o x q) ndarray of coordinate positions for dimension 2
    y -- (z x o x q) ndarray of coordinate positions for dimension 3

    Outputs:
    f -- (n x o x q) ndarray of function values at positions (x,y)
    """

    A = p[0]
    f = A * _unit_gaussian(p[[1, 4]], x) \
        * _unit_gaussian(p[[2, 5]], y) \
        * _unit_gaussian(p[[3, 6]], z)
    return f


def gaussian3d(p, x, y, z):
    """
    Required Arguments:
    p -- (m) [A,x0,y0,z0,FWHMx,FWHMy,FWHMz,c0,c1x,c1y,c1z]
    x -- (n x o x q) ndarray of coordinate positions for dimension 1
    y -- (n x o x q) ndarray of coordinate positions for dimension 2
    y -- (z x o x q) ndarray of coordinate positions for dimension 3

    Outputs:
    f -- (n x o x q) ndarray of function values at positions (x,y,z)
    """

    bg0 = p[7]
    bg1x = p[8]
    bg1y = p[9]
    bg1z = p[10]

    f = _gaussian3d_no_bg(p[:5], x, y)+(bg0+bg1x*x+bg1y*y+bg1z*z)
    return f


# =============================================================================
# Mutlipeak
# =============================================================================

def _mpeak_1d_no_bg(p, x, pktype, num_pks):
    """
    Required Arguments:
    p -- (m x u) list of peak parameters for number of peaks
         where m is the number of parameters per peak
             - "gaussian" and "lorentzian" - 3
             - "pvoigt" - 4
             - "split_pvoigt" - 6
    x -- (n) ndarray of coordinate positions for dimension 1
    pktype -- string, type of analytic function; current options are
        "gaussian","lorentzian","pvoigt" (psuedo voigt), and
        "split_pvoigt" (split psuedo voigt)
    num_pks -- integer 'u' indicating the number of pks, must match length of p

    Outputs:
    f -- (n) ndarray of function values at positions (x)
    """

    f = np.zeros(len(x))

    if pktype == 'gaussian' or pktype == 'lorentzian':
        p_fit = np.reshape(p[:3*num_pks], [num_pks, 3])
    elif pktype == 'pvoigt':
        p_fit = np.reshape(p[:4*num_pks], [num_pks, 4])
    elif pktype == 'split_pvoigt':
        p_fit = np.reshape(p[:6*num_pks], [num_pks, 6])

    for ii in np.arange(num_pks):
        if pktype == 'gaussian':
            f = f+_gaussian1d_no_bg(p_fit[ii], x)
        elif pktype == 'lorentzian':
            f = f+_lorentzian1d_no_bg(p_fit[ii], x)
        elif pktype == 'pvoigt':
            f = f+_pvoigt1d_no_bg(p_fit[ii], x)
        elif pktype == 'split_pvoigt':
            f = f+_split_pvoigt1d_no_bg(p_fit[ii], x)

    return f


def mpeak_1d(p, x, pktype, num_pks, bgtype=None):
    """
    Required Arguments:
    p -- (m x u) list of peak parameters for number of peaks where m is the
         number of parameters per peak
             "gaussian" and "lorentzian" - 3
             "pvoigt" - 4
             "split_pvoigt" - 6
    x -- (n) ndarray of coordinate positions for dimension 1
    pktype -- string, type of analytic function that will be used;
    current options are "gaussian","lorentzian","pvoigt" (psuedo voigt), and
    "split_pvoigt" (split psuedo voigt)
    num_pks -- integer 'u' indicating the number of pks, must match length of p
    pktype -- string, background functions, available options are "constant",
    "linear", and "quadratic"

    Outputs:
    f -- (n) ndarray of function values at positions (x)
    """
    f = _mpeak_1d_no_bg(p, x, pktype, num_pks)

    if bgtype == 'linear':
        f = f+p[-2]+p[-1]*x  # c0=p[-2], c1=p[-1]
    elif bgtype == 'constant':
        f = f+p[-1]  # c0=p[-1]
    elif bgtype == 'quadratic':
        f = f+p[-3]+p[-2]*x+p[-1]*x**2  # c0=p[-3], c1=p[-2], c2=p[-1],

    return f


@numba_njit_if_available(cache=True, nogil=True)
def _mixing_factor_pv(fwhm_g, fwhm_l):
    """
    @AUTHOR:  Saransh Singh, Lawrence Livermore National Lab,
    saransh1@llnl.gov
    @DATE: 05/20/2020 SS 1.0 original
           01/29/2021 SS 2.0 updated to depend only on fwhm of profile
           P. Thompson, D.E. Cox & J.B. Hastings, J. Appl. Cryst.,20,79-83,
           1987
    @DETAILS: calculates the mixing factor eta to best approximate voight
    peak shapes
    """
    fwhm = fwhm_g**5 + 2.69269 * fwhm_g**4 * fwhm_l + \
        2.42843 * fwhm_g**3 * fwhm_l**2 + \
        4.47163 * fwhm_g**2 * fwhm_l**3 +\
        0.07842 * fwhm_g * fwhm_l**4 +\
        fwhm_l**5

    fwhm = fwhm**0.20
    eta = 1.36603 * (fwhm_l/fwhm) - \
        0.47719 * (fwhm_l/fwhm)**2 + \
        0.11116 * (fwhm_l/fwhm)**3
    if eta < 0.:
        eta = 0.
    elif eta > 1.:
        eta = 1.

    return eta, fwhm

@numba_njit_if_available(cache=True, nogil=True)
def pvoight_wppf(uvw,
                 p,
                 xy,
                 xy_sf,
                 shkl,
                 eta_mixing,
                 tth,
                 dsp,
                 hkl,
                 strain_direction_dot_product,
                 is_in_sublattice,
                 tth_list):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 03/22/2021 SS 1.0 original
    @details pseudo voight peak profile for WPPF
    """
    gamma_ani_sqr = _anisotropic_peak_broadening(
        shkl, hkl)
    fwhm_g = _gaussian_fwhm(uvw, p,
        gamma_ani_sqr,
        eta_mixing,
        tth, dsp)
    fwhm_l = _lorentzian_fwhm(xy, xy_sf,
        gamma_ani_sqr, eta_mixing,
        tth, dsp,
        strain_direction_dot_product,
        is_in_sublattice)

    n, fwhm = _mixing_factor_pv(fwhm_g, fwhm_l)

    Ag = 0.9394372787/fwhm  # normalization factor for unit area
    Al = 1.0/np.pi          # normalization factor for unit area

    g = Ag*_unit_gaussian(np.array([tth, fwhm]), tth_list)
    l = Al*_unit_lorentzian(np.array([tth, fwhm]), tth_list)
    
    return n*l + (1.0-n)*g

@numba_njit_if_available(cache=True, nogil=True)
def _func_h(tau, tth_r):
    cph =  np.cos(tth_r - tau)
    ctth = np.cos(tth_r)
    return np.sqrt( (cph/ctth)**2 - 1.)

@numba_njit_if_available(cache=True, nogil=True)
def _func_W(HoL, SoL, tau, tau_min, tau_infl, tth):

    if(tth < np.pi/2.):
        if tau >= 0. and tau <= tau_infl:
            res = 2.0*min(HoL,SoL)
        elif tau > tau_infl and tau <= tau_min:
            res = HoL+SoL+_func_h(tau,tth)
        else:
            res = 0.0
    else:
        if tau <= 0. and tau >= tau_infl:
            res = 2.0*min(HoL,SoL)
        elif tau < tau_infl and tau >= tau_min:
            res = HoL+SoL+_func_h(tau,tth)
        else:
            res = 0.0
    return res

@numba_njit_if_available(cache=True, nogil=True)
def pvfcj(uvw,
          p,
          xy,
          xy_sf,
          shkl,
          eta_mixing,
          tth,
          dsp,
          hkl,
          strain_direction_dot_product,
          is_in_sublattice,
          tth_list,
          HoL,
          SoL,
          xn,
          wn):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 04/02/2021 SS 1.0 original
    @details pseudo voight convolved with the slit functions
    xn, wn are the Gauss-Legendre weights and abscissae
    supplied using the scipy routine
    """

    # angle of minimum
    tth_r = np.radians(tth)
    ctth = np.cos(tth_r)

    arg = ctth*np.sqrt(((HoL+SoL)**2+1.))
    cinv = np.arccos(arg)
    tau_min = tth_r - cinv

    # two theta of inflection point
    arg = ctth*np.sqrt(((HoL-SoL)**2+1.))
    cinv = np.arccos(arg)
    tau_infl = tth_r - cinv

    tau = tau_min*xn

    cx = np.cos(tau)
    res = np.zeros(tth_list.shape)
    den = 0.0

    for i in np.arange(tau.shape[0]):
        x = tth_r-tau[i]
        xx = tau[i]

        W = _func_W(HoL,SoL,xx,tau_min,tau_infl,tth_r)
        h = _func_h(xx, tth_r)
        fact = wn[i]*(W/h/cx[i])
        den += fact

        pv = pvoight_wppf(uvw,
             p,
             xy,
             xy_sf,
             shkl,
             eta_mixing,
             np.degrees(x),
             dsp,
             hkl,
             strain_direction_dot_product,
             is_in_sublattice,
             tth_list)
        res += pv*fact

    res = np.sin(tth_r)*res/den/4./HoL/SoL
    a = np.trapz(res, tth_list)
    return res/a

@numba_njit_if_available(cache=True, nogil=True)
def _calc_alpha(alpha, tth):
    a0, a1 = alpha
    return a0 + a1*np.tan(np.radians(0.5*tth))


@numba_njit_if_available(cache=True, nogil=True)
def _calc_beta(beta, tth):
    b0, b1 = beta
    return b0 + b1*np.tan(np.radians(0.5*tth))


#@numba_njit_if_available(cache=True, nogil=True)
def _gaussian_pink_beam(alpha,
                        beta,
                        fwhm_g,
                        tth,
                        tth_list):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 03/22/2021 SS 1.0 original
    @details the gaussian component of the pink beam peak profile
    obtained by convolution of gaussian with normalized back to back
    exponentials. more details can be found in
    Von Dreele et. al., J. Appl. Cryst. (2021). 54, 3–6
    """
    del_tth = tth_list - tth
    sigsqr = fwhm_g**2
    f1 = alpha*sigsqr + 2.0*del_tth
    f2 = beta*sigsqr - 2.0*del_tth
    f3 = np.sqrt(2.0)*fwhm_g
    u = 0.5*alpha*f1
    v = 0.5*beta*f2
    y = (f1-del_tth)/f3
    z = f2/f3

    return (0.5*(alpha*beta)/(alpha + beta)) \
        * (np.exp(u)*erfc(y) + \
            np.exp(v)*erfc(z))


#@numba_njit_if_available(cache=True, nogil=True)
def _lorentzian_pink_beam(alpha,
                          beta,
                          fwhm_l,
                          tth,
                          tth_list):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 03/22/2021 SS 1.0 original
    @details the lorentzian component of the pink beam peak profile
    obtained by convolution of gaussian with normalized back to back
    exponentials. more details can be found in
    Von Dreele et. al., J. Appl. Cryst. (2021). 54, 3–6
    """
    del_tth = tth_list - tth
    p = -alpha*del_tth + 1j * 0.5*alpha*fwhm_l
    q = -beta*del_tth + 1j * 0.5*beta*fwhm_l

    f1 = np.imag(np.exp(p)*exp1(p))
    f2 = np.imag(np.exp(q)*exp1(q))

    return -(alpha*beta)/(np.pi*(alpha + beta)) * (f1 + f2)

#@numba_njit_if_available(cache=True, nogil=True)
def pvoight_pink_beam(alpha,
                      beta,
                      uvw,
                      p,
                      xy,
                      xy_sf,
                      shkl,
                      eta_mixing,
                      tth,
                      dsp,
                      hkl,
                      strain_direction_dot_product,
                      is_in_sublattice,
                      tth_list):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 03/22/2021 SS 1.0 original
    @details compute the pseudo voight peak shape for the pink
    beam using von dreele's function
    """
    alpha_exp = _calc_alpha(alpha, tth)
    beta_exp = _calc_beta(beta, tth)

    gamma_ani_sqr = _anisotropic_peak_broadening(
        shkl, hkl)

    fwhm_g = _gaussian_fwhm(uvw, p,
    gamma_ani_sqr,
    eta_mixing,
    tth, dsp)
    fwhm_l = _lorentzian_fwhm(xy, xy_sf,
        gamma_ani_sqr, eta_mixing,
        tth, dsp,
        strain_direction_dot_product,
        is_in_sublattice)

    n = _mixing_factor_pv(fwhm_g, fwhm_l)

    g = _gaussian_pink_beam(alpha_exp, beta_exp,
                            fwhm_g, tth, tth_list)
    l = _lorentzian_pink_beam(alpha_exp, beta_exp,
                              fwhm_l, tth, tth_list)
    return n*l + (1.0-n)*g

@numba_njit_if_available(cache=True, nogil=True, parallel=True)
def computespectrum(uvw,
                 p,
                 xy,
                 xy_sf,
                 shkl,
                 eta_mixing,
                 HL,
                 SL,
                 tth,
                 dsp,
                 hkl,
                 strain_direction_dot_product,
                 is_in_sublattice,
                 tth_list,
                 Iobs, 
                 xn, 
                 wn):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 03/31/2021 SS 1.0 original
    @details compute the spectrum given all the input parameters.
    moved outside of the class to allow numba implementation
    this is called for multiple wavelengths and phases to generate
    the final spectrum
    """
    spec = np.zeros(tth_list.shape)
    nref = np.min(np.array([Iobs.shape[0],
        tth.shape[0],
        dsp.shape[0],hkl.shape[0]]))
    for ii in prange(nref):
        II = Iobs[ii]
        t = tth[ii]
        d = dsp[ii]
        g = hkl[ii]
        # pv = pvoight_wppf(uvw,p,xy,
        #          xy_sf,shkl,eta_mixing,
        #          t,d,g,
        #          strain_direction_dot_product,
        #          is_in_sublattice,
        #          tth_list)
        pv = pvfcj(uvw,p,xy,xy_sf,
                   shkl,eta_mixing,
                   t,d,g,
                   strain_direction_dot_product,
                   is_in_sublattice,
                   tth_list,
                   HL,SL,xn,wn)
        spec += II * pv
    return spec

@numba_njit_if_available(cache=True, nogil=True)
def calc_Iobs(uvw,
            p,
            xy,
            xy_sf,
            shkl,
            eta_mixing,
            HL,
            SL,
            xn,
            wn,
            tth,
            dsp,
            hkl,
            strain_direction_dot_product,
            is_in_sublattice,
            tth_list,
            Icalc,
            spectrum_expt,
            spectrum_sim):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 03/31/2021 SS 1.0 original
    @details compute Iobs for each reflection given all parameters.
    moved outside of the class to allow numba implementation
    this is called for multiple wavelengths and phases to generate
    the final spectrum
    """
    Iobs = np.zeros(tth.shape)
    nref = np.min(np.array([Icalc.shape[0],
        tth.shape[0],
        dsp.shape[0],hkl.shape[0]]))
    for ii in np.arange(nref):
        Ic = Icalc[ii]
        t = tth[ii]
        d = dsp[ii]
        g = hkl[ii]
        # pv = pvoight_wppf(uvw,p,xy,
        #          xy_sf,shkl,eta_mixing,
        #          t,d,g,
        #          strain_direction_dot_product,
        #          is_in_sublattice,
        #          tth_list)
        pv = pvfcj(uvw,p,xy,xy_sf,
                   shkl,eta_mixing,
                   t,d,g,
                   strain_direction_dot_product,
                   is_in_sublattice,
                   tth_list,
                   HL,SL,xn,wn)

        y = Ic * pv
        yo = spectrum_expt[:,1]
        yc = spectrum_sim[:,1]
        mask = yc != 0.
        """
        @TODO if yc has zeros in it, then this
        the next line will not like it. need to
        address that
        @ SS 03/02/2021 the mask shold fix it
        """
        Iobs[ii] = np.trapz(yo[mask] * y[mask] /
                                 yc[mask], tth_list[mask])

    return Iobs

@numba_njit_if_available(cache=True, nogil=True)
def calc_rwp(spectrum_sim,
             spectrum_expt,
             weights,
             P):
    """
    @author Saransh Singh, Lawrence Livermore National Lab
    @date 03/31/2021 SS 1.0 original
    @details calculate the rwp given all the input parameters.
    moved outside of the class to allow numba implementation
    P : number of independent parameters in fitting
    """
    err = spectrum_sim - spectrum_expt

    errvec = np.sqrt(weights * err[:,1]**2)

    """ weighted sum of square """
    wss = np.trapz(weights * err[:,1]**2, spectrum_expt[:,0])
    den = np.trapz(weights * spectrum_sim[:,1] **
                   2, spectrum_expt[:,0])

    """ standard Rwp i.e. weighted residual """
    Rwp = np.sqrt(wss/den)

    """ number of observations to fit i.e. number of data points """
    N = spectrum_sim.shape[0]

    if den > 0.:
        if (N-P)/den > 0:
            Rexp = np.sqrt((N-P)/den)
        else:
            Rexp = 0.0
    else:
        Rexp = np.inf

    # Rwp and goodness of fit parameters
    if Rexp > 0.:
        gofF = (Rwp / Rexp)**2
    else:
        gofF = np.inf

    return errvec[~np.isnan(errvec)], Rwp, gofF