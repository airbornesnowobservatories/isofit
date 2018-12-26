#! /usr/bin/env python3
#
#  Copyright 2018 California Institute of Technology
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# ISOFIT: Imaging Spectrometer Optimal FITting
# Author: David R Thompson, david.r.thompson@jpl.nasa.gov
#

import scipy as s
from scipy.io import loadmat, savemat
from scipy.interpolate import interp1d
from scipy.signal import convolve
from common import eps, srf, load_wavelen, resample_spectrum
from numpy.random import multivariate_normal as mvn


# Max. wavelength difference (nm) that does not trigger expensive resampling
wl_tol = 0.01 


class Instrument:

    def __init__(self, config):
        """A model of the spectrometer instrument, including spectral 
        response and noise covariance matrices. Noise is typically calculated
        from a parametric model, fit for the specific instrument.  It is a 
        function of the radiance level."""

        # If needed, skip first index column and/or convert to nanometers
        self.wl_init, self.fwhm_init = load_wavelen(config['wavelength_file'])
        self.n_chan = len(self.wl_init)
        self.bounds, self.scale, self.statevec, self.init_val = [], [], [], []
        if 'statevector' in config:
            for key in config['statevector']:
                 self.statevec.append(key)
                 self.init_val.append(config['statevector'][key]['init'])
                 self.bounds.append(config['statevector'][key]['bounds'])
                 self.scale.append(config['statevector'][key]['scale'])
        self.n_state = len(self.statevec)

        # noise specified as parametric model.
        if 'SNR' in config:
            self.model_type = 'SNR'
            self.snr = float(config['SNR'])
        else:
            self.noise_file = config['noise_file']
            if self.noise_file.endswith('.txt'):
                # parametric version
                self.model_type = 'parametric'
                coeffs = s.loadtxt(
                    self.noise_file, delimiter=' ', comments='#')
                p_a = interp1d(coeffs[:, 0], coeffs[:, 1],
                               fill_value='extrapolate')
                p_b = interp1d(coeffs[:, 0], coeffs[:, 2],
                               fill_value='extrapolate')
                p_c = interp1d(coeffs[:, 0], coeffs[:, 3],
                               fill_value='extrapolate')
                self.noise = s.array([[p_a(w), p_b(w), p_c(w)]
                                      for w in self.wl_init])
            elif self.noise_file.endswith('.mat'):
                # full FPA
                self.model_type = 'pushbroom'
                D = loadmat(self.noise_file)
                self.ncols = D['columns'][0, 0]
                if self.n_chan != s.sqrt(D['bands'][0, 0]):
                    raise ValueError(
                        'Noise model does not match wavelength # bands')
                cshape = ((self.ncols, self.n_chan, self.n_chan))
                self.covs = D['covariances'].reshape(cshape)
        self.integrations = config['integrations']

        # Variables not retrieved = always start with relative cal
        self.bvec = ['Cal_Relative_%04i' % int(w) for w in self.wl_init] + \
            ['Cal_Spectral', 'Cal_Stray_SRF']
        self.bval = s.zeros(self.n_chan+2)
        if 'unknowns' in config:
            special_unknowns = ['wavelength_calibration_uncertainty']
            # Radiometric uncertainties combine via Root Sum Square...
            for key, val in config['unknowns'].items():
                if key in special_unknowns: 
                    continue 
                elif type(val) is str:
                    u = s.loadtxt(val, comments='#')
                    if (len(u.shape) > 0 and u.shape[1] > 1):
                        u = u[:, 1]
                else:
                    u = s.ones(self.n_chan) * val
                self.bval[:self.n_chan] = self.bval[:self.n_chan] + pow(u,2)
            self.bval[:self.n_chan] = s.sqrt(self.bval[:self.n_chan])

            # Now handle spectral uncertainties
            if 'wavelength_calibration_uncertainty' in config['unknowns']:
                self.bval[-2] = \
                    config['unknowns']['wavelength_calibration_uncertainty']
            if 'stray_srf_uncertainty' in config:
                self.bval[-1] = config['unknowns']['stray_srf_uncertainty']

        self.calibration_fixed = (not ('FWHM_SCL' in self.statevec)) and \
            (not ('WL_SHIFT' in self.statevec))

    def xa(self):
        '''Mean of prior distribution, calculated at state x. '''
        return self.init_val.copy()

    def Sa(self):
        '''Covariance of prior distribution. (diagonal)'''
        if self.n_state == 0: 
           return s.zeros((0,0), dtype=float)
        return s.diagflat(pow(self.prior_sigma, 2))

    def Sy(self, meas, geom):
        """ Calculate measurement error covariance.
           Input: meas, the instrument measurement
           Returns: Sy, the measurement error covariance due to instrument noise"""

        if self.model_type == 'SNR':
            bad = meas < 1e-5
            meas[bad] = 1e-5
            nedl = (1.0 / self.snr) * meas
            return pow(s.diagflat(nedl), 2)

        elif self.model_type == 'parametric':
            nedl = abs(
                self.noise[:, 0]*s.sqrt(self.noise[:, 1]+meas)+self.noise[:, 2])
            nedl = nedl/s.sqrt(self.integrations)
            return pow(s.diagflat(nedl), 2)

        elif self.model_type == 'pushbroom':
            if geom.pushbroom_column is None:
                C = s.squeeze(self.covs.mean(axis=0))
            else:
                C = self.covs[geom.pushbroom_column, :, :]
            return C / s.sqrt(self.integrations)

    def dmeas_dinstrument(self, x_instrument, wl_hi, rdn_hi):
        """Jacobian of measurement  with respect to instrument 
           variables.  We use finite differences for now.""" 

        dmeas_dinstrument = s.zeros((self.n_chan, self.n_state), dtype=float)
        if self.n_state == 0:
          return dmeas_dinstrument

        meas = self.sample(x_instrument, wl_hi, rdn_hi)
        for ind in range(self.statevec):
            x_instrument_perturb = x_instrument.copy()
            x_instrument_perturb[ind] = x_instrument_perturb[ind]+eps
            meas_perturb = self.sample(x_instrument_perturb, wl_hi, rdn_hi)
            dmeas_dinstrument[:,ind] = (meas_perturb - meas) / eps
        return dmeas_dinstrument

    def dmeas_dinstrumentb(self, x_instrument, wl_hi, rdn_hi):
        """Jacobian of radiance with respect to NOT RETRIEVED instrument 
           variables (relative miscalibration error).
           Input: meas, a vector of size n_chan
           Returns: Kb_instrument, a matrix of size 
            [n_measurements x nb_instrument]"""

        # Uncertainty due to radiometric calibration
        meas = self.sample(x_instrument, wl_hi, rdn_hi)
        dmeas_dinstrument = s.hstack((s.diagflat(meas),
                s.zeros((self.n_chan,2))))

        # Uncertainty due to spectral calibration 
        if self.bval[-2] > 1e-6:
          dmeas_dinstrument[:,-2] = self.sample(x_instrument, wl_hi,
                  s.hstack((s.diff(rdn_hi), s.array([0]))))

        # Uncertainty due to spectral stray light 
        if self.bval[-1] > 1e-6:
          ssrf = srf(s.arange(-10,11), 0, 4) 
          blur = convolve(meas, ssrf, mode='same') 
          dmeas_dinstrument[:,-1] = blur - meas
          
        return dmeas_dinstrument

    def sample(self, x_instrument, wl_hi, rdn_hi):
        """ Apply instrument sampling to a radiance spectrum"""
        if self.calibration_fixed and all((self.wl_init - wl_hi) < wl_tol):
            return rdn_hi
        wl, fwhm = self.calibration(x_instrument)
        if rdn_hi.ndim == 1:
            return resample_spectrum(rdn_hi, wl_hi, wl, fwhm)
        else:
            resamp = [resample_spectrum(r, wl_hi, wl, fwhm) for r in rdn_hi]
            return s.array(resamp)

    def simulate_measurement(self, meas, geom):
        """ Simulate a measurement by the given sensor, for a true radiance."""
        Sy = self.Sy(meas, geom)
        mu = s.zeros(meas.shape)
        rdn_sim = meas + mvn(mu, Sy)
        return rdn_sim

    def calibration(self, x_instrument):
        """ Calculate the measured wavelengths"""
        wl, fwhm = self.wl_init, self.fwhm_init
        if 'FWHM_SCL' in self.statevec:
            ind = self.statevec.index('FWHM_SCL')
            fwhm = fwhm + x_instrument[ind]
        if 'WL_SHIFT' in self.statevec:
            ind = self.statevec.index('WL_SHIFT')
            wl = self.wl_init + x_instrument[ind]
        return wl, fwhm


