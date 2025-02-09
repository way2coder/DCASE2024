# Contains routines for labels creation, features extraction and normalization, 
#

from cls_vid_features import VideoFeatures
import parameters
from PIL import Image
import os
import numpy as np
import scipy.io.wavfile as wav
from sklearn import preprocessing
import joblib
from IPython import embed
import matplotlib.pyplot as plot
import librosa
plot.switch_backend('agg')
import shutil
import math
import wave
import contextlib
import cv2
from spafe.fbanks import mel_fbanks, gammatone_fbanks, bark_fbanks
import time 
import torch
import csv
import pandas as pd
import sys
import random 
import shutil
import pickle

def nCr(n, r):
    return math.factorial(n) // math.factorial(r) // math.factorial(n-r)


class FeatureClass:
    def __init__(self, params, is_eval=False):
        """

        :param params: parameters dictionary
        :param is_eval: if True, does not load dataset labels.
        """
        # Input directories
        self._is_eval = is_eval
        self._feat_label_dir = params['feat_label_dir']
        self._dataset_dir = params['dataset_dir'] 
        self._dataset = params['dataset']
        self._dataset_combination = '{}_{}'.format(params['data_type'], 'eval' if is_eval else 'dev')  # foa_dev 
        self._aud_dir = os.path.join(self._dataset_dir, self._dataset_combination) if self._dataset in ['STARSS2023'] else self._dataset_dir #'../Dataset/STARSS2023\\foa_dev'

        self._subset = params['subset']

        self._desc_dir = None if is_eval else (os.path.join(self._dataset_dir, 'metadata_dev') if self._dataset in ['STARSS2023'] else self._dataset_dir) # '../Dataset/STARSS2023\\metadata_dev'

        self._vid_dir = os.path.join(self._dataset_dir, 'video_{}'.format('eval' if is_eval else 'dev')) # 
        # Output directories
        self._data_augmentation = params['data_augmentation']
        self._sound_scaper = params['sound_scaper']
        self._preprocessing_type = params['preprocessing_type']

        self._feat_dir = None
        self._feat_dir_norm = None
        
        self._vid_feat_dir = None
        # video feature extraction
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.pretrained_vid_model = VideoFeatures().to(self.device)  # 确保模型在正确的设备上

        # Local parameters

        self._fs = params['fs']  # 24000, 44100? 
        self._hop_len_s = params['hop_len_s'] # 0.01
        self._hop_len = int(self._fs * self._hop_len_s) # 240

        self._label_hop_len_s_STARSS = params['label_hop_len_s_STARSS']
        self._label_hop_len_s = params['label_hop_len_s']  # 0.1 second
        self._label_hop_len = int(self._fs * self._label_hop_len_s) # 2400  sample
        self._label_frame_res = self._fs / float(self._label_hop_len) # 10.0 
        self._segment_length= params['segment_length']

        self._win_len = 2 * self._hop_len # 480
        self._nfft = self._next_greater_power_of_2(self._win_len) # 512
        self._feature_sequence_length = params['feature_sequence_length']  # 

        self._data_type = params['data_type'] # foa
        self._eps = 1e-8
        self._nb_channels = 4

        # self._multi_accdoa = params['multi_accdoa']  # bool
        self._output_format = params['output_format']

        self._filter_type = params['filter']  # "mel", "gammatone", "bark"

        self._use_salsalite = params['use_salsalite'] # bool
        if self._use_salsalite and self._data_type=='mic': #  _use_salsalite is valid only when the dataset config is mic
            # Initialize the spatial feature constants
            self._lower_bin = np.int(np.floor(params['fmin_doa_salsalite'] * self._nfft / np.float(self._fs)))
            self._lower_bin = np.max((1, self._lower_bin))
            self._upper_bin = np.int(np.floor(np.min((params['fmax_doa_salsalite'], self._fs//2)) * self._nfft / np.float(self._fs)))


            # Normalization factor for salsalite
            c = 343
            self._delta = 2 * np.pi * self._fs / (self._nfft * c)
            self._freq_vector = np.arange(self._nfft//2 + 1)
            self._freq_vector[0] = 1
            self._freq_vector = self._freq_vector[None, :, None]  # 1 x n_bins x 1

            # Initialize spectral feature constants
            self._cutoff_bin = np.int(np.floor(params['fmax_spectra_salsalite'] * self._nfft / np.float(self._fs)))
            assert self._upper_bin <= self._cutoff_bin, 'Upper bin for doa featurei {} is higher than cutoff bin for spectrogram {}!'.format()
            self._nb_mel_bins = self._cutoff_bin - self._lower_bin
        else:
            self._nb_mel_bins = params['nb_mel_bins']
            # decide which mel wts will be used by the parameter mel filter type
            if self._filter_type == 'mel':
                self._mel_wts = librosa.filters.mel(sr=self._fs, n_fft=self._nfft, n_mels=self._nb_mel_bins).T
            elif self._filter_type == 'gammatone':
                self._mel_wts, _ = gammatone_fbanks.gammatone_filter_banks(nfilts=self._nb_mel_bins, nfft=self._nfft, fs=self._fs, scale='descendant')
                self._mel_wts = self._mel_wts.T
            elif self._filter_type == 'bark':
                self._mel_wts, _ = bark_fbanks.bark_filter_banks(nfilts=self._nb_mel_bins, nfft=self._nfft, fs=self._fs)
                self._mel_wts = self._mel_wts.T            
            else:
                raise ValueError("Unsupported filter type: {}".format(self._filter_type))
    
        # Sound event classes dictionary
        self._nb_unique_classes = params['unique_classes'] #13
        self._classes_mapping = params['classes_mapping']

        self._filewise_frames = {}  # 文件名： 特征时间帧，以及label时间帧
        self._label_dir = self.get_label_dir()  # feat_label_hnet\foa_dev_multi_accdoa_0.1_label
        self._new_label_dir = self.get_new_label_dir()
        # breakpoint()

    def get_frame_stats(self):
        # Initialized the self._filewise_frames = {}, what this dictionary stored is {'fold4_room23_mix001': [3035, 607]}
        #  if {'fold4_room23_mix001': [3035, 607]}, then it indictates that the length of the audio is 60.7 s 607 ms
        if len(self._filewise_frames) != 0:
            return

        print('Computing frame stats:')
        print('\t\taud_dir {}\n\t\tdesc_dir {}\n\t\tfeat_dir {}'.format(
            self._aud_dir, self._desc_dir, self._feat_dir))
        
        
        if self._dataset in ['STARSS2023']:
            for sub_folder in os.listdir(self._aud_dir): 
                if self._sound_scaper == False and  sub_folder == 'foa_generate':
                    continue
                loc_aud_folder = os.path.join(self._aud_dir, sub_folder)   #'../Dataset/STARSS2023\\foa_dev\\dev-test-sony'
                for file_cnt, file_name in enumerate(os.listdir(loc_aud_folder)):
                    wav_filename = '{}.wav'.format(file_name.split('.')[0])
                    with contextlib.closing(wave.open(os.path.join(loc_aud_folder, wav_filename), 'r')) as f:
                        audio_len = f.getnframes()
                    nb_feat_frames = int(audio_len / float(self._hop_len))   # 1456800 / 480
                    nb_label_frames = int(audio_len / float(self._label_hop_len))   #  1456800 / 2400 
                    file_name = file_name.split('.')[0]
                    self._filewise_frames[file_name] = [nb_feat_frames, nb_label_frames] # {'fold4_room23_mix001': [3035, 607]}
                    if self._data_augmentation == True:
                        self._filewise_frames[file_name + '_channel_swapping'] = [nb_feat_frames, nb_label_frames]
                        self._filewise_frames[file_name + '_frequency_masking'] = [nb_feat_frames, nb_label_frames]
            if self._subset :
                sample_file_list_path = self.get_sampled_feat_dir_norm()

                with open(sample_file_list_path, 'rb') as f:
                    file_list = pickle.load(f)

                # breakpoint()
                stripped_file_list = [filename.rstrip('.npy') for filename in file_list]
                filtered_filewise_frames = {}
                for key, value in self._filewise_frames.items():
                    if key in stripped_file_list:
                        filtered_filewise_frames[key] = value
                self._filewise_frames = filtered_filewise_frames

                

        elif self._dataset in ['ANSYN', 'ASIRD', 'L3DAS21']:
            for sub_folder in os.listdir(self._aud_dir):
                if 'desc' not in sub_folder:
                    continue 
                # 
                ov_split =  sub_folder.split('desc')[-1]
                loc_aud_folder = os.path.join(self._aud_dir, 'wav'+ ov_split+'_30db')   #'../Dataset/ANSYN\\wav_ov1_split1_30db'

                for file_cnt, file_name in enumerate(os.listdir(loc_aud_folder)): 
                    wav_filename = '{}.wav'.format(file_name.split('.')[0])
                    with contextlib.closing(wave.open(os.path.join(loc_aud_folder, wav_filename), 'r')) as f:
                        audio_len = f.getnframes()
                    nb_feat_frames = int(audio_len / float(self._hop_len))   # 1456800 / 480
                    nb_label_frames = int(audio_len / float(self._label_hop_len))   #  1456800 / 2400 
                    file_name_key = file_name.split('.')[0] + ov_split    # 'test_0_desc_30_100_ov1_split1'
                    self._filewise_frames[file_name_key] = [nb_feat_frames, nb_label_frames] # {'fold4_room23_mix001': [3035, 607]}
                    if self._data_augmentation == True:
                        self._filewise_frames[file_name + '_channel_swapping'] = [nb_feat_frames, nb_label_frames]
                        self._filewise_frames[file_name + '_frequency_masking'] = [nb_feat_frames, nb_label_frames]
        
        return

    def _load_audio(self, audio_path): # load wav file from audio_path
        fs, audio = wav.read(audio_path)
        audio = audio[:, :self._nb_channels] / 32768.0 + self._eps  # dtype int16 -> double64
        return audio, fs

    # INPUT FEATURES
    @staticmethod
    def _next_greater_power_of_2(x):
        return 2 ** (x - 1).bit_length()

    def _spectrogram(self, audio_input, _nb_frames):
        # n_fft = 512,  hop_length = 0.01, self._win_len = 0.02
        _nb_ch = audio_input.shape[1]
        nb_bins = self._nfft // 2
        spectra = []
        for ch_cnt in range(_nb_ch):
            stft_ch = librosa.core.stft(np.asfortranarray(audio_input[:, ch_cnt]), n_fft=self._nfft, hop_length=self._hop_len,
                                        win_length=self._win_len, window='hann')
            spectra.append(stft_ch[:, :_nb_frames])
        return np.array(spectra).T
 
    def _get_mel_spectrogram(self, linear_spectra): # get mel spectrogram
        
        mel_feat = np.zeros((linear_spectra.shape[0], self._nb_mel_bins, linear_spectra.shape[-1]))
        for ch_cnt in range(linear_spectra.shape[-1]):
            mag_spectra = np.abs(linear_spectra[:, :, ch_cnt])**2
            mel_spectra = np.dot(mag_spectra, self._mel_wts)
            log_mel_spectra = librosa.power_to_db(mel_spectra)
            mel_feat[:, :, ch_cnt] = log_mel_spectra
        mel_feat = mel_feat.transpose((0, 2, 1)).reshape((linear_spectra.shape[0], -1))
        return mel_feat

    def _get_foa_intensity_vectors(self, linear_spectra):
        
        W = linear_spectra[:, :, 0]
        I = np.real(np.conj(W)[:, :, np.newaxis] * linear_spectra[:, :, 1:])
        E = self._eps + (np.abs(W)**2 + ((np.abs(linear_spectra[:, :, 1:])**2).sum(-1)) / 3.0)

        I_norm = I / E[:, :, np.newaxis]
        I_norm_mel = np.transpose(np.dot(np.transpose(I_norm, (0, 2, 1)), self._mel_wts), (0, 2, 1))
        foa_iv = I_norm_mel.transpose((0, 2, 1)).reshape((linear_spectra.shape[0], self._nb_mel_bins * 3))
        if np.isnan(foa_iv).any():
            print('Feature extraction is generating nan outputs')
            exit()
        return foa_iv

    def _get_gcc(self, linear_spectra):
        gcc_channels = nCr(linear_spectra.shape[-1], 2)
        gcc_feat = np.zeros((linear_spectra.shape[0], self._nb_mel_bins, gcc_channels))
        cnt = 0
        for m in range(linear_spectra.shape[-1]):
            for n in range(m+1, linear_spectra.shape[-1]):
                R = np.conj(linear_spectra[:, :, m]) * linear_spectra[:, :, n]
                cc = np.fft.irfft(np.exp(1.j*np.angle(R)))
                cc = np.concatenate((cc[:, -self._nb_mel_bins//2:], cc[:, :self._nb_mel_bins//2]), axis=-1)
                gcc_feat[:, :, cnt] = cc
                cnt += 1
        return gcc_feat.transpose((0, 2, 1)).reshape((linear_spectra.shape[0], -1))

    def _get_salsalite(self, linear_spectra):
        # Adapted from the official SALSA repo- https://github.com/thomeou/SALSA
        # spatial features
        phase_vector = np.angle(linear_spectra[:, :, 1:] * np.conj(linear_spectra[:, :, 0, None]))
        phase_vector = phase_vector / (self._delta * self._freq_vector)
        phase_vector = phase_vector[:, self._lower_bin:self._cutoff_bin, :]
        phase_vector[:, self._upper_bin:, :] = 0
        phase_vector = phase_vector.transpose((0, 2, 1)).reshape((phase_vector.shape[0], -1))

        # spectral features
        linear_spectra = np.abs(linear_spectra)**2
        for ch_cnt in range(linear_spectra.shape[-1]):
            linear_spectra[:, :, ch_cnt] = librosa.power_to_db(linear_spectra[:, :, ch_cnt], ref=1.0, amin=1e-10, top_db=None)
        linear_spectra = linear_spectra[:, self._lower_bin:self._cutoff_bin, :]
        linear_spectra = linear_spectra.transpose((0, 2, 1)).reshape((linear_spectra.shape[0], -1))

        return np.concatenate((linear_spectra, phase_vector), axis=-1)

    def _get_spectrogram_for_file(self, audio_filename):
        
        audio_in, fs = self._load_audio(audio_filename)  # ((1072800, 4), 24000)  (-1, 1) + 1e-8 

        nb_feat_frames = int(len(audio_in) / float(self._hop_len))   # 2235 = 1072800 / 480
        nb_label_frames = int(len(audio_in) / float(self._label_hop_len)) # 447 = 1072800/2400
        self._filewise_frames[os.path.basename(audio_filename).split('.')[0]] = [nb_feat_frames, nb_label_frames]

        audio_spec = self._spectrogram(audio_in, nb_feat_frames)  # (2235, 513, 4) time, frequency, channel
        
        return audio_spec 
    
    # OUTPUT LABELs
    def get_polar_labels_for_file(self, _desc_file, _nb_label_frames):
        '''
        Reads description file and returns classification based SED labels and regression based DOA labels in cartesian

        :param _desc_file: metadata description file
        :param _nb_label_frames: the number of the frames based on the label resolution  of one file, typically a number of hundreds
        :return: label_mat: of dimension [nb_frames, 4*max_classes], max_classes each for event_activity, azimuth, elevation, distance
        '''
        se_label = np.zeros((_nb_label_frames, self._nb_unique_classes))    
        azimuth_label = np.zeros((_nb_label_frames, self._nb_unique_classes))
        elevation_label = np.zeros((_nb_label_frames, self._nb_unique_classes))
        dist_label = np.zeros((_nb_label_frames, self._nb_unique_classes))    # 

        for frame_ind, active_event_list in _desc_file.items():
            if frame_ind < _nb_label_frames:
                for active_event in active_event_list:  
                    # [8, 0, 14.0, 0.0, 392.0]   azimuth elevation -> rad 
                    se_label[frame_ind, active_event[0]] = 1
                    azimuth_label[frame_ind, active_event[0]] = np.deg2rad(active_event[-3])
                    elevation_label[frame_ind, active_event[0]] = np.deg2rad(active_event[-2])
                    dist_label[frame_ind, active_event[0]] = active_event[-1]/100

        label_mat = np.concatenate((se_label, azimuth_label, elevation_label, dist_label), axis=1) # 607, 13 * 5 
        return label_mat
    

    # OUTPUT LABELS
    def get_cartesian_labels_for_file(self, _desc_file, _nb_label_frames):
        """
        Reads description file and returns classification based SED labels and regression based DOA labels in cartesian

        :param _desc_file: metadata description file
        :param _nb_label_frames: the number of the frames based on the label resolution  of one file, typically a hundreds number 
        :return: label_mat: of dimension [nb_frames, 5*max_classes], max_classes each for event_activity,x, y, z axis,distance
        """

        # If using Hungarian net set default DOA value to a fixed value greater than 1 for all axis. We are choosing a fixed value of 10
        # If not using Hungarian net use a deafult DOA, which is a unit vector. We are choosing (x, y, z) = (0, 0, 1)
        se_label = np.zeros((_nb_label_frames, self._nb_unique_classes))    
        x_label = np.zeros((_nb_label_frames, self._nb_unique_classes))
        y_label = np.zeros((_nb_label_frames, self._nb_unique_classes))
        z_label = np.zeros((_nb_label_frames, self._nb_unique_classes))     # (607, 13)
        dist_label = np.zeros((_nb_label_frames, self._nb_unique_classes))    # 
        
        for frame_ind, active_event_list in _desc_file.items():
            if frame_ind < _nb_label_frames:
                for active_event in active_event_list:  
                    #active event [8, 0, 0.9702957262759965, 0.24192189559966773, 0.0, 392.0]
                    se_label[frame_ind, active_event[0]] = 1
                    x_label[frame_ind, active_event[0]] = active_event[2]
                    y_label[frame_ind, active_event[0]] = active_event[3]
                    z_label[frame_ind, active_event[0]] = active_event[4]
                    dist_label[frame_ind, active_event[0]] = active_event[5]/100

        label_mat = np.concatenate((se_label, x_label, y_label, z_label, dist_label), axis=1) # 607, 13 * 5 
        return label_mat

    # OUTPUT LABELS
    def get_adpit_labels_for_file(self, _desc_file, _nb_label_frames):
        """
        Reads description file and returns classification based SED labels and regression based DOA labels
        for multi-ACCDOA with Auxiliary Duplicating Permutation Invariant Training (ADPIT)

        :param _desc_file: metadata description file
        :return: label_mat: of dimension [nb_frames, 6, 4(=act+XYZ), max_classes]
        """

        se_label = np.zeros((_nb_label_frames, 6, self._nb_unique_classes))  # [nb_frames, 6, max_classes]
        x_label = np.zeros((_nb_label_frames, 6, self._nb_unique_classes))   # (607, 6, 13)
        y_label = np.zeros((_nb_label_frames, 6, self._nb_unique_classes))
        z_label = np.zeros((_nb_label_frames, 6, self._nb_unique_classes))
        dist_label = np.zeros((_nb_label_frames, 6, self._nb_unique_classes))

        for frame_ind, active_event_list in _desc_file.items():
            if frame_ind < _nb_label_frames:
                active_event_list.sort(key=lambda x: x[0])  # sort for ov from the same class 
                # Sort the event according to the class number increasingly
                active_event_list_per_class = []
                for i, active_event in enumerate(active_event_list):
                    active_event_list_per_class.append(active_event)
                    if i == len(active_event_list) - 1:  # if the last
                        if len(active_event_list_per_class) == 1:  # if no ov from the same class
                            # a0----
                            active_event_a0 = active_event_list_per_class[0]
                            se_label[frame_ind, 0, active_event_a0[0]] = 1
                            x_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[2]
                            y_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[3]
                            z_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[4]
                            dist_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[5]/100.
                        elif len(active_event_list_per_class) == 2:  # if ov with 2 sources from the same class
                            # --b0--
                            active_event_b0 = active_event_list_per_class[0]
                            se_label[frame_ind, 1, active_event_b0[0]] = 1
                            x_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[2]
                            y_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[3]
                            z_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[4]
                            dist_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[5]/100.
                            # --b1--
                            active_event_b1 = active_event_list_per_class[1]
                            se_label[frame_ind, 2, active_event_b1[0]] = 1
                            x_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[2]
                            y_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[3]
                            z_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[4]
                            dist_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[5]/100.
                        else:  # if ov with more than 2 sources from the same class
                            # ----c0
                            active_event_c0 = active_event_list_per_class[0]
                            se_label[frame_ind, 3, active_event_c0[0]] = 1
                            x_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[2]
                            y_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[3]
                            z_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[4]
                            dist_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[5]/100.
                            # ----c1
                            active_event_c1 = active_event_list_per_class[1]
                            se_label[frame_ind, 4, active_event_c1[0]] = 1
                            x_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[2]
                            y_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[3]
                            z_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[4]
                            dist_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[5]/100.
                            # ----c2
                            active_event_c2 = active_event_list_per_class[2]
                            se_label[frame_ind, 5, active_event_c2[0]] = 1
                            x_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[2]
                            y_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[3]
                            z_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[4]
                            dist_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[5]/100.

                    elif active_event[0] != active_event_list[i + 1][0]:  # if the next is not the same class
                        if len(active_event_list_per_class) == 1:  # if no ov from the same class
                            # a0----
                            active_event_a0 = active_event_list_per_class[0]
                            se_label[frame_ind, 0, active_event_a0[0]] = 1
                            x_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[2]
                            y_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[3]
                            z_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[4]
                            dist_label[frame_ind, 0, active_event_a0[0]] = active_event_a0[5]/100.
                        elif len(active_event_list_per_class) == 2:  # if ov with 2 sources from the same class
                            # --b0--
                            active_event_b0 = active_event_list_per_class[0]
                            se_label[frame_ind, 1, active_event_b0[0]] = 1
                            x_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[2]
                            y_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[3]
                            z_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[4]
                            dist_label[frame_ind, 1, active_event_b0[0]] = active_event_b0[5]/100.
                            # --b1--
                            active_event_b1 = active_event_list_per_class[1]
                            se_label[frame_ind, 2, active_event_b1[0]] = 1
                            x_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[2]
                            y_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[3]
                            z_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[4]
                            dist_label[frame_ind, 2, active_event_b1[0]] = active_event_b1[5]/100.
                        else:  # if ov with more than 2 sources from the same class
                            # ----c0
                            active_event_c0 = active_event_list_per_class[0]
                            se_label[frame_ind, 3, active_event_c0[0]] = 1
                            x_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[2]
                            y_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[3]
                            z_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[4]
                            dist_label[frame_ind, 3, active_event_c0[0]] = active_event_c0[5]/100.
                            # ----c1
                            active_event_c1 = active_event_list_per_class[1]
                            se_label[frame_ind, 4, active_event_c1[0]] = 1
                            x_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[2]
                            y_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[3]
                            z_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[4]
                            dist_label[frame_ind, 4, active_event_c1[0]] = active_event_c1[5]/100.
                            # ----c2
                            active_event_c2 = active_event_list_per_class[2]
                            se_label[frame_ind, 5, active_event_c2[0]] = 1
                            x_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[2]
                            y_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[3]
                            z_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[4]
                            dist_label[frame_ind, 5, active_event_c2[0]] = active_event_c2[5]/100.
                        active_event_list_per_class = []

        label_mat = np.stack((se_label, x_label, y_label, z_label, dist_label), axis=2)  # [nb_frames, 6, 5(=act+XYZ+dist), max_classes]
        return label_mat

    # ------------------------------- EXTRACT AUDIO FEATURE AND PREPROCESS IT -------------------------------
    def extract_file_feature_frequency_masking(self, _arg_in):
        '''
        random frequency masking from 2 strategies:
            1. 1/4 of number of frequency bins masking on frequency axis, 1/4 of feature_sequence_length time masking on time axis, 10 times masking in one file
            2. 1/8 of number of frequency bins masking on frequency axis, 1/2 of feature_sequence_length time masking on time axis, 10 times masking in one file 
        '''
        _file_cnt, _wav_path, _feat_path = _arg_in
        file_name = f"{os.path.basename(_wav_path).split('.')[0]}.csv"
        wav_filename = '{}.wav'.format(file_name.split('.')[0])
        label_path = os.path.join(self._label_dir, '{}_frequency_masking.npy'.format(wav_filename.split('.')[0]))
        desc_path = os.path.join(self._new_label_dir, '{}_frequency_masking.csv'.format(wav_filename.split('.')[0]))
        shutil.copy(os.path.join(self._new_label_dir, file_name), desc_path)
        if os.path.exists(_feat_path) and os.path.exists(label_path) and os.path.exists(desc_path) :
            print(f"Skipping {_feat_path} {label_path} as the .npy file of features and labels are already extracted.")
        else:
            spect = self._get_spectrogram_for_file(_wav_path) #  (2235, 513, 4) time, frequency, channel
            number_frequency_bins = spect.shape[1]
            feature_sequence_length = self._feature_sequence_length
            print('the shape of spectrogram is ',spect.shape, ' and the data type of spect is ', spect.dtype)
            # frequency masking on spectrogram, random 
            for _ in range(10):  # 10 times grid masking  
                # Strategy 1
                if np.random.rand() > 0.5: 
                    freq_mask_start = np.random.randint(0, number_frequency_bins // 4 * 3 )
                    time_mask_start = np.random.randint(0, feature_sequence_length // 4 * 3)
                    spect[time_mask_start:time_mask_start + feature_sequence_length // 4, freq_mask_start:freq_mask_start + number_frequency_bins // 4, :] = 0
                # Strategy 2
                else:
                    freq_mask_start = np.random.randint(0, number_frequency_bins // 8 * 7)
                    time_mask_start = np.random.randint(0, feature_sequence_length // 2)
                    spect[time_mask_start:time_mask_start + feature_sequence_length // 2, freq_mask_start:freq_mask_start + number_frequency_bins // 8, :] = 0
    
            # extract mel
            if not self._use_salsalite:
                mel_spect = self._get_mel_spectrogram(spect) # get mel from spectrogram, (2235, 256)
            feat = None
            if self._data_type == 'foa': 
                # extract intensity vectors from spect 
                foa_iv = self._get_foa_intensity_vectors(spect) # 2235, 192
                feat = np.concatenate((mel_spect, foa_iv), axis=-1) # 2235, 448 = 2235, 64*7 = T, 64, 7
            elif self._data_type == 'mic':
                if self._use_salsalite:
                    feat = self._get_salsalite(spect)
                else: 
                    # extract gcc
                    gcc = self._get_gcc(spect)
                    feat = np.concatenate((mel_spect, gcc), axis=-1)
            else:
                print('ERROR: Unknown dataset format {}'.format(self._data_type))
                exit()
            
            if feat is not None:
                print('frequency masking feature {}: {}, {} has been generated '.format(_file_cnt, os.path.basename(_wav_path), feat.shape))

                np.save(_feat_path, feat)  # ../Dataset/STARSS2023/feat_label_hnet/foa_dev\\fold4_room23_mix002.npy
            # extract corresponding label file of this wave file

            # self.get_frame_stats()

    
            print(f'Extracting label file of {_wav_path}:')
            # if self._dataset in ['STARSS2023']:
            #     for sub_folder in os.listdir(self._desc_dir):
            #         loc_desc_folder = os.path.join(self._desc_dir, sub_folder)
            print(f'new label dir :{self._new_label_dir}')
            # for file_cnt, file_name in enumerate(os.listdir(self._new_label_dir)):   
                # for each file(like fold4_room23_mix001.csv), process it into label hop frames according to the self._filewise_frames

            nb_label_frames = self._filewise_frames[file_name.split('.')[0]][1]  # 607 
            desc_file_polar = self.load_output_format_file(os.path.join(self._new_label_dir, file_name))  
                #'../Dataset/STARSS2023\\metadata_dev\\dev-test-sony\\fold4_room23_mix001.csv'

            desc_file = self.convert_output_format_polar_to_cartesian(desc_file_polar)    # len(desc_file)
            if self._output_format == 'multi_accdoa': 
                label_mat = self.get_adpit_labels_for_file(desc_file, nb_label_frames)
            elif self._output_format == 'single_accdoa':
                label_mat = self.get_cartesian_labels_for_file(desc_file, nb_label_frames)  # (607, 65)
            elif self._output_format == 'polar':
                label_mat = self.get_polar_labels_for_file(desc_file_polar, nb_label_frames) #             
            np.save(label_path, label_mat)
            print('{}: {}, {}'.format(_file_cnt, label_path, label_mat.shape))

        

    def extract_file_feature_channel_swapping(self, _arg_in):
        '''
        There are 4 channels in one seperate audio file, H1, H2, H3, H4 in order, and W, X, Y, Z corresponds to channel H1, H3, H4, H2.
        And 16 transformations are used in this augmentation method, according to report[SOUND EVENT LOCALIZATION AND DETECTION USING FOA DOMAIN SPATIAL AUGMENTATION  Technical Report]
        '''
        _file_cnt, _wav_path, _feat_path = _arg_in
        file_name = f"{os.path.basename(_wav_path).split('.')[0]}.csv"
        wav_filename = '{}.wav'.format(file_name.split('.')[0])
        label_path = os.path.join(self._label_dir, '{}_channel_swapping.npy'.format(wav_filename.split('.')[0]))
        desc_path = os.path.join(self._new_label_dir, '{}_channel_swapping.csv'.format(wav_filename.split('.')[0]))

        if os.path.exists(_feat_path) and os.path.exists(label_path) and os.path.exists(desc_path):
            print(f"Skipping {_feat_path} as features and labels are already extracted.")
        else:
            # load audio file, store the attribute of this file in self._filewise_frames
            audio_in, fs = self._load_audio(_wav_path)  # ((1072800, 4), 24000)  (-1, 1) + 1e-8 
            print(audio_in.shape)
            nb_feat_frames = int(len(audio_in) / float(self._hop_len))   # 2235 = 1072800 / 480
            nb_label_frames = int(len(audio_in) / float(self._label_hop_len)) # 447 = 1072800/2400
            self._filewise_frames[os.path.basename(_wav_path).split('.')[0]] = [nb_feat_frames, nb_label_frames]

            # read annotation file in csv format
            print(f'Read label file of {_wav_path}:')
            print(f'new label dir :{self._new_label_dir}')
            file_name = f"{os.path.basename(_wav_path).split('.')[0]}.csv"
            print(f'{file_name} has been read successfully')
            wav_filename = '{}.wav'.format(file_name.split('.')[0])

            nb_label_frames = self._filewise_frames[file_name.split('.')[0]][1]  # 607 
            desc_file_path = os.path.join(self._new_label_dir, file_name)
            # desc_file_polar = self.load_output_format_file(os.path.join(self._new_label_dir, file_name))  
            #'../Dataset/STARSS2023\\metadata_dev\\dev-test-sony\\fold4_room23_mix001.csv'            
            # Core function: random channel swapping on this audio file
            audio_in, desc_file_polar = self._random_channel_swapping(audio_in, desc_file_path)
            self.write_output_format_file(desc_path, desc_file_polar, 'dict_to_polar')
            spect = self._spectrogram(audio_in, nb_feat_frames)  # (2235, 513, 4) time, frequency, channel
            feat = None
            if not self._use_salsalite:
                mel_spect = self._get_mel_spectrogram(spect) # get mel from spectrogram, (2235, 256)        
            if self._data_type == 'foa': 
                # extract intensity vectors from spect 
                foa_iv = self._get_foa_intensity_vectors(spect) # 2235, 192
                feat = np.concatenate((mel_spect, foa_iv), axis=-1) # 2235, 448 = 2235, 64*7 = T, 64, 7
            elif self._data_type == 'mic':
                if self._use_salsalite:
                    feat = self._get_salsalite(spect)
                else: 
                    # extract gcc
                    gcc = self._get_gcc(spect)
                    feat = np.concatenate((mel_spect, gcc), axis=-1)
            if feat is not None:
                print('{}: {}, {}'.format(_file_cnt, os.path.basename(_wav_path), feat.shape))

                np.save(_feat_path, feat)  # ../Dataset/STARSS2023/feat_label_hnet/foa_dev\\fold4_room23_mix002.npy

            desc_file = self.convert_output_format_polar_to_cartesian(desc_file_polar)    # len(desc_file)

            if self._output_format == 'multi_accdoa': 
                label_mat = self.get_adpit_labels_for_file(desc_file, nb_label_frames)
            elif self._output_format == 'single_accdoa':
                label_mat = self.get_cartesian_labels_for_file(desc_file, nb_label_frames)  # (607, 65)
            elif self._output_format == 'polar':
                label_mat = self.get_polar_labels_for_file(desc_file_polar, nb_label_frames) #             
            np.save(label_path, label_mat)
            print('{}: {}, {}'.format(_file_cnt, label_path, label_mat.shape))

            if self._output_format == 'multi_accdoa': 
                label_mat = self.get_adpit_labels_for_file(desc_file, nb_label_frames)
            else:
                print(f'{self._output_format} is not supported currently.')
                sys.exit()
    
    def _random_channel_swapping(self, audio_in, desc_file_path):
        '''
        sub function of extract_file_feature_channel_swapping,

        return: 
            audio_in: ndarray format after random transformation
            desc_file_polar: dict type, 
        '''
        # W = audio_in[:, 0]
        # X = audio_in[:, 3]
        # Y = audio_in[:, 1]
        # Z = audio_in[:, 2]
        transformation_idx = random.randint(1, 16)
        
        # print(type(desc_file), desc_file)
        # print(desc_file)
        # access elevation and azimuth from desc_file
        if transformation_idx == 1:  # Rotate azi by -pi/2, Y->X, -X->Y
            temp = audio_in[:, 3].copy()
            audio_in[:, 3] = audio_in[:, 1]
            audio_in[:, 1] = -temp
        elif transformation_idx == 2:  # Original
            pass
        elif transformation_idx == 3:  # Rotate azi by +pi/2, X->Y, -Y->X
            temp = audio_in[:, 1].copy()
            audio_in[:, 1] = audio_in[:, 3]
            audio_in[:, 3] = -temp

        elif transformation_idx == 4:  # Rotate azi by +pi, -X -> X, -Y->Y
            audio_in[:, 3] = - audio_in[:, 3]
            audio_in[:, 1] = - audio_in[:, 1]
        elif transformation_idx == 5:  #  Rotate azi by -pi/2, flip ele, Y->X, -X->Y, -Z->Z
            temp = X = audio_in[:, 3].copy()
            audio_in[:, 3] = audio_in[:, 1]
            audio_in[:, 1] = -temp
            audio_in[:, 2] = -audio_in[:, 2]

        elif transformation_idx == 6:  # Azi remains unchanged,flip ele: -Z->Z
            audio_in[:, 2] = - audio_in[:, 2]
        elif transformation_idx == 7:  # Rotate azi by +pi/2 and flip ele:-Y->X, X->Y,-Z->Z
            temp = audio_in[:, 3]
            audio_in[:, 3] = -audio_in[:, 1]
            audio_in[:, 1] = temp
            audio_in[:, 2] = -audio_in[:, 2]        
        elif transformation_idx == 8:  # Rotate azi by +pi and flip ele:-X->X, -Y->Y, -Z->Z
            audio_in[:, 3] = - audio_in[:, 3]
            audio_in[:, 2] = - audio_in[:, 2]
            audio_in[:, 1] = - audio_in[:, 1]
        elif transformation_idx == 9:  # Rotate -azi by -pi/2: Y->X, -X->Y
            temp = audio_in[:, 3]
            audio_in[:, 3] = audio_in[:, 1]
            audio_in[:, 1] = -temp
        elif transformation_idx == 10:  # Rotate -azi: -Y -> Y
            audio_in[:, 1] = -audio_in[:, 1]
        elif transformation_idx == 11:  # Rotate -azi by +pi/2:Y->X, X->Y
            temp = audio_in[:, 3]
            audio_in[:, 3] = audio_in[:, 1]
            audio_in[:, 1] = temp 
        elif transformation_idx == 12:  # Rotate -azi by +pi:-X->X
            audio_in[:, 3] = -audio_in[:, 3]
        elif transformation_idx == 13:  # Rotate -azi by -pi/2 and flip ele:Y->X,-X->Y
            temp = audio_in[:, 3]
            audio_in[:, 3] = audio_in[:, 1]
            audio_in[:, 1] = -temp 
        elif transformation_idx == 14:  # Rotate -azi and flip ele:-Y->Y, -Z->Z
            audio_in[:, 1] = -audio_in[:, 1]
            audio_in[:, 2] = -audio_in[:, 2]
        elif transformation_idx == 15:  # Rotate -azi by +pi/2, flip ele:Y->X, X->Y, -Z->Z
            temp = audio_in[:, 3]
            audio_in[:, 3] = audio_in[:, 1]
            audio_in[:, 1] = temp 
            audio_in[:, 2] = -audio_in[:, 2]
        elif transformation_idx == 16:  # Rotate -azi by pi and flip ele:-X->X, -Z->Z
            audio_in[:, 3] = -audio_in[:, 3]
            audio_in[:, 2] = -audio_in[:, 2]
        
        desc_file_polar = self.load_output_format_file_transformation(desc_file_path,transformation_idx)

        return audio_in, desc_file_polar
        

    def extract_file_feature(self, _arg_in): # 提取单个wav文件的特征
        _file_cnt, _wav_path, _feat_path = _arg_in
        file_name = f"{os.path.basename(_wav_path).split('.')[0]}.csv"
        print(file_name)
        wav_filename = '{}.wav'.format(file_name.split('.')[0])
        label_path = os.path.join(self._label_dir, '{}.npy'.format(wav_filename.split('.')[0]))
        desc_path = os.path.join(self._new_label_dir, '{}.csv'.format(wav_filename.split('.')[0]))
 
        if os.path.exists(_feat_path) and os.path.exists(label_path) and os.path.exists(desc_path):
            print(f"Skipping {_feat_path} {label_path} as the .npy file of features and labels are already extracted.")

        else:
            spect = self._get_spectrogram_for_file(_wav_path) #  (2235, 513, 4)
            # extract mel
            if not self._use_salsalite:
                mel_spect = self._get_mel_spectrogram(spect) # get mel from spectrogram, (2235, 256)

            feat = None
            if self._data_type == 'foa': 
                # extract intensity vectors from spect 
                foa_iv = self._get_foa_intensity_vectors(spect) # 2235, 192
                feat = np.concatenate((mel_spect, foa_iv), axis=-1) # 2235, 448 = 2235, 64*7 = T, 64, 7
            elif self._data_type == 'mic':
                if self._use_salsalite:
                    feat = self._get_salsalite(spect)
                else: 
                    # extract gcc
                    gcc = self._get_gcc(spect)
                    feat = np.concatenate((mel_spect, gcc), axis=-1)
            else:
                print('ERROR: Unknown dataset format {}'.format(self._data_type))
                exit()
            
            # extract labels
            
            if feat is not None:
                print('{}: {}, {}'.format(_file_cnt, os.path.basename(_wav_path), feat.shape))

                np.save(_feat_path, feat)  # ../Dataset/STARSS2023/feat_label_hnet/foa_dev\\fold4_room23_mix002.npy
            
            # print(f'Extracting label file of {_wav_path}:')
            # if self._dataset in ['STARSS2023']:
            #     for sub_folder in os.listdir(self._desc_dir):
            #         loc_desc_folder = os.path.join(self._desc_dir, sub_folder)
            # print(f'new label dir :{self._new_label_dir}')
            # for file_cnt, file_name in enumerate(os.listdir(self._new_label_dir)):   
                # for each file(like fold4_room23_mix001.csv), process it into label hop frames according to the self._filewise_frames

            nb_label_frames = self._filewise_frames[file_name.split('.')[0]][1]  # 607 
            desc_file_polar = self.load_output_format_file(os.path.join(self._new_label_dir, file_name))  
                #'../Dataset/STARSS2023\\metadata_dev\\dev-test-sony\\fold4_room23_mix001.csv'

            desc_file = self.convert_output_format_polar_to_cartesian(desc_file_polar)    # len(desc_file)
            if self._output_format == 'multi_accdoa': 
                label_mat = self.get_adpit_labels_for_file(desc_file, nb_label_frames)
            elif self._output_format == 'single_accdoa':
                label_mat = self.get_cartesian_labels_for_file(desc_file, nb_label_frames)  # (607, 65)
            elif self._output_format == 'polar':
                label_mat = self.get_polar_labels_for_file(desc_file_polar, nb_label_frames) #             
            np.save(label_path, label_mat)
            print('{}: {}, {}'.format(_file_cnt, label_path, label_mat.shape))

    # def extract_all_feature_augmentation(self):
    #     if self._data_augmentation is not True:
    #         return 
    #     self._feat_dir = self.get_unnormalized_feat_dir()
    #     create_folder(self._feat_dir)
    #     start_s = time.time()
    #     print('Extracting augmentation features:')
    #     print('\t\taud_dir {}\n\t\tdesc_dir {}\n\t\tfeat_dir {}'.format(
    #         self._aud_dir, self._desc_dir, self._feat_dir))  # ('../Dataset/STARSS2023\\foa_dev', '../Dataset/STARSS2023\\metadata_dev', '../Dataset/STARSS2023/feat_label_hnet/foa_dev_mel')
    #     arg_list = [] 
    #     for sub_folder in os.listdir(self._aud_dir): # dev-test-sony, dev-test-tau, dev-train-sony
    #         loc_aud_folder = os.path.join(self._aud_dir, sub_folder)  
    #         for file_cnt, file_name in enumerate(os.listdir(loc_aud_folder)):
    #             wav_filename = '{}.wav'.format(file_name.split('.')[0])
    #             wav_path = os.path.join(loc_aud_folder, wav_filename) # ../Dataset/STARSS2023\\foa_dev\\dev-test-sony\\fold4_room23_mix001.wav
    #             feat_path = os.path.join(self._feat_dir, '{}.npy'.format(wav_filename.split('.')[0]))  # ../Dataset/STARSS2023/feat_label_hnet/foa_dev\\fold4_room23_mix001.npy
    #             # process only when the file is not exsit
    #             feat_path_frequency_masking = os.path.join(self._feat_dir, f'{wav_filename.split('.')[0]}_frequency_masking.npy')
    #             feat_path_channel_swapping = os.path.join(self._feat_dir, f'{wav_filename.split('.')[0]}_channel_swapping.npy')

    #             if not os.path.exists(feat_path):
    #                 self.extract_file_feature_frequency_masking((file_cnt, wav_path, feat_path)) # 提取单个wav文件的特征
    #                 self.extract_file_feature_channel_swapping((file_cnt, wav_path, feat_path))
    #             else:
    #                 print(f"Skipping {feat_path} as features are already extracted.")
    #             arg_list.append((file_cnt, wav_path, feat_path)) 


    def extract_all_features_and_labels(self): 
        # setting up folders
        self._feat_dir = self.get_unnormalized_feat_dir() # '../Dataset/STARSS2023/feat_label_hnet/foa_dev_mel'
        self.get_frame_stats() # some preprocessing procedure of label file generation
        create_folder(self._feat_dir)
        create_folder(self._label_dir)
        from multiprocessing import Pool
        import time
        start_s = time.time()
        # extraction starts
        print('Extracting spectrogram:')
        print('\t\taud_dir {}\n\t\tdesc_dir {}\n\t\tfeat_dir {}'.format(
            self._aud_dir, self._desc_dir, self._feat_dir))  # ('../Dataset/STARSS2023\\foa_dev', '../Dataset/STARSS2023\\metadata_dev', '../Dataset/STARSS2023/feat_label_hnet/foa_dev_mel')
        arg_list = []    
    # if self._dataset in ['STARSS2023']:
        for sub_folder in os.listdir(self._aud_dir): # dev-test-sony, dev-test-tau, dev-train-sony
            if sub_folder != 'foa':
                continue 
            loc_aud_folder = os.path.join(self._aud_dir, sub_folder)  
            for file_cnt, file_name in enumerate(os.listdir(loc_aud_folder)):
                file_prefix = file_name.split('.')[0]
                if file_prefix not in self._filewise_frames:
                    continue 
                wav_filename = '{}.wav'.format(file_prefix)
                wav_path = os.path.join(loc_aud_folder, wav_filename) # ../Dataset/STARSS2023\\foa_dev\\dev-test-sony\\fold4_room23_mix001.wav
                feat_path = os.path.join(self._feat_dir, '{}.npy'.format(wav_filename.split('.')[0]))  # ../Dataset/STARSS2023/feat_label_hnet/foa_dev\\fold4_room23_mix001.npy
                # process only when the file is not exsit
                feat_path_frequency_masking = os.path.join(self._feat_dir, f'{wav_filename.split(".")[0]}_frequency_masking.npy')
                feat_path_channel_swapping = os.path.join(self._feat_dir, f'{wav_filename.split(".")[0]}_channel_swapping.npy')
                
                self.extract_file_feature((file_cnt, wav_path, feat_path)) # 提取vanilla wav文件的特征
                if self._data_augmentation is True and file_name[4] is '3':
                    self.extract_file_feature_frequency_masking((file_cnt, wav_path, feat_path_frequency_masking)) # 提取单个wav文件的特征
                    self.extract_file_feature_channel_swapping((file_cnt, wav_path, feat_path_channel_swapping)) # 提取单个wav文件的特征
                else:
                    print(f'this procedure donnot involve data augmentation ')  
                
                arg_list.append((file_cnt, wav_path, feat_path)) 

        # elif self._dataset in ['ANSYN', 'ASIRD', 'L3DAS21']:
        #     for sub_folder in os.listdir(self._aud_dir): # dev-test-sony, dev-test-tau, dev-train-sony
        #         if 'wav' not in sub_folder: 
        #             continue 
        #         ov_split = sub_folder.split('_')[1] + '_' + sub_folder.split('_')[2]
        #         loc_aud_folder = os.path.join(self._aud_dir, sub_folder)  
        #         for file_cnt, file_name in enumerate(os.listdir(loc_aud_folder)):
        #             wav_filename = '{}.wav'.format(file_name.split('.')[0])
        #             wav_path = os.path.join(loc_aud_folder, wav_filename) # ../Dataset/STARSS2023\\foa_dev\\dev-test-sony\\fold4_room23_mix001.wav
        #             feat_path = os.path.join(self._feat_dir, '{}_{}.npy'.format(wav_filename.split('.')[0], ov_split))  # ../Dataset/STARSS2023/feat_label_hnet/foa_dev\\fold4_room23_mix001.npy
        #             # process only when the file is not exsit
        #             if not os.path.exists(feat_path):
        #                 self.extract_file_feature((file_cnt, wav_path, feat_path)) # 提取单个wav文件的特征
        #             else:
        #                 print(f"Skipping {feat_path} as features are already extracted.")
        #             arg_list.append((file_cnt, wav_path, feat_path)) 


        # with Pool() as pool:
        #     result = pool.map(self.extract_file_feature, iterable=arg_list)
        #     pool.close()
        #     pool.join()
        print(time.time()-start_s)

    def preprocess_features(self):
        # Setting up folders and filenames
        self._feat_dir = self.get_unnormalized_feat_dir() # ../Dataset/STARSS2023/feat_label_hnet/foa_dev_mel
        self._feat_dir_norm = self.get_normalized_feat_dir()  # '../Dataset/STARSS2023/feat_label_hnet/foa_dev_mel_norm'
        create_folder(self._feat_dir_norm) 
        normalized_features_wts_file = self.get_normalized_wts_file() #../Dataset/STARSS2023/feat_label_hnet/foa_wts
        spec_scaler = None

        # pre-processing starts, wts is needed only when _is_eval is true;
        if self._is_eval:
            spec_scaler = joblib.load(normalized_features_wts_file)
            print('Normalized_features_wts_file: {}. Loaded.'.format(normalized_features_wts_file))

        else:
            print('Estimating weights for normalizing feature files:')
            print('\t\tfeat_dir: {}'.format(self._feat_dir))

            spec_scaler = preprocessing.StandardScaler()
            for file_cnt, file_name in enumerate(os.listdir(self._feat_dir)):
                print('{}: {}'.format(file_cnt, file_name))
                feat_file = np.load(os.path.join(self._feat_dir, file_name))
                spec_scaler.partial_fit(feat_file)
                del feat_file
            joblib.dump(
                spec_scaler,
                normalized_features_wts_file
            )
            print('Normalized_features_wts_file: {}. Saved.'.format(normalized_features_wts_file))

        print('Normalizing feature files:')
        print('\t\tfeat_dir_norm {}'.format(self._feat_dir_norm))
        for file_cnt, file_name in enumerate(os.listdir(self._feat_dir)):
            print('{}: {}'.format(file_cnt, file_name))
            feat_file = np.load(os.path.join(self._feat_dir, file_name)) 
            feat_file = spec_scaler.transform(feat_file)
            np.save(
                os.path.join(self._feat_dir_norm, file_name),
                feat_file
            )
            del feat_file

        print('normalized files written to {}'.format(self._feat_dir_norm))

    # ------------------------------- EXTRACT LABELS AND PREPROCESS IT -------------------------------
    def generate_new_labels(self):
        
        self.get_frame_stats() # 
        self._new_label_dir = self.get_new_label_dir()  # feat_label_hnet\foa_dev_multi_accdoa_0.1_label
        print(f'Generate new labels of {self._label_hop_len_s*1000}ms resolution and new classes mapping')
        print(f'\t\tnew label dir {self._new_label_dir} for resolution of {self._label_hop_len_s*1000}')
        create_folder(self._new_label_dir)  
        if self._dataset in ['STARSS2023']:
            for sub_folder in os.listdir(self._desc_dir):
                if sub_folder != 'foa':
                    continue 
                loc_desc_folder = os.path.join(self._desc_dir, sub_folder)
                for file_cnt, file_name in enumerate(os.listdir(loc_desc_folder)):  
                    # for each file(like fold4_room23_mix001.csv), process it into label hop frames according to the self._filewise_frames
                    file_prefix = file_name.split('.')[0]
                    if file_prefix not in self._filewise_frames:
                        continue
                    wav_filename = '{}.wav'.format(file_prefix)

                    nb_label_frames = self._filewise_frames[file_prefix][1]  # already tailored for the 
                    
                    csv_df = self.generate_standard_label_df(loc_desc_folder, file_name)
                    new_csv_path = os.path.join(self._new_label_dir, file_name)
                    csv_df.to_csv(new_csv_path, index=False, header=False)
                    print(f'{new_csv_path} has been generated')

        # elif self._dataset in ['ANSYN', 'ASIRD', 'L3DAS21']:
        #     for sub_folder in os.listdir(self._dataset_dir):
        #         if 'desc' not in sub_folder: 
        #             continue 
        #         # modified_subfolder = os.path.join(self._label_dir, sub_folder)
        #         # create_folder(modified_subfolder)
        #         # subfolder 'desc_ov1_split2'
        #         ov_split = sub_folder.split('desc')[-1]
        #         relative_wave_folder = 'wav'+ov_split +'_30db'
        #         loc_desc_folder = os.path.join(self._desc_dir, sub_folder)   # '../Dataset/ANSYN\\desc_ov1_split1' 
        #         for file_cnt, file_name in enumerate(os.listdir(loc_desc_folder)): 

        #             wav_filename = '{}.wav'.format(file_name.split('.')[0])
        #             file_name_key = file_name.split('.')[0] + ov_split 
        #             nb_label_frames = self._filewise_frames[file_name_key][1] 
        #             csv_df = self.generate_standard_label_df(loc_desc_folder,file_name)
        #             file_name = wav_filename.split('.')[0] + ov_split +'.csv'
        #             new_csv_path = os.path.join(self._new_label_dir, file_name)
        #             csv_df.to_csv(new_csv_path, index=False, header=False)

        
    
    def extract_all_labels(self): 
        self.get_frame_stats() # 
        self._label_dir = self.get_label_dir()  # feat_label_hnet\foa_dev_multi_accdoa_0.1_label
        self._new_label_dir = self.get_new_label_dir()

        print('Extracting labels:')
        print('\t\taud_dir {}\n\t\tdesc_dir {}\n\t\tlabel_dir {}'.format(
            self._aud_dir, self._desc_dir, self._label_dir))
        create_folder(self._label_dir)
        # if self._dataset in ['STARSS2023']:
        #     for sub_folder in os.listdir(self._desc_dir):
        #         loc_desc_folder = os.path.join(self._desc_dir, sub_folder)
        print(f'new label dir :{self._new_label_dir}')
        for file_cnt, file_name in enumerate(os.listdir(self._new_label_dir)):   
            # for each file(like fold4_room23_mix001.csv), process it into label hop frames according to the self._filewise_frames
            wav_filename = '{}.wav'.format(file_name.split('.')[0])

            nb_label_frames = self._filewise_frames[file_name.split('.')[0]][1]  # 607 
            desc_file_polar = self.load_output_format_file(os.path.join(self._new_label_dir, file_name))  
                #'../Dataset/STARSS2023\\metadata_dev\\dev-test-sony\\fold4_room23_mix001.csv'

            desc_file = self.convert_output_format_polar_to_cartesian(desc_file_polar)    # len(desc_file)
            if self._output_format == 'multi_accdoa': 
                label_mat = self.get_adpit_labels_for_file(desc_file, nb_label_frames)
            elif self._output_format == 'single_accdoa':
                label_mat = self.get_cartesian_labels_for_file(desc_file, nb_label_frames)  # (607, 65)
            elif self._output_format == 'polar':
                label_mat = self.get_polar_labels_for_file(desc_file_polar, nb_label_frames) # 
            print('{}: {}, {}'.format(file_cnt, file_name, label_mat.shape))
            label_path = os.path.join(self._label_dir, '{}.npy'.format(wav_filename.split('.')[0]))
            if os.path.exists(label_path):
                print(f"Skipping {label_path} as features are already extracted.")
            else:
                np.save(label_path, label_mat)


        #TODO?
        
    # def preprocess_labels(self):
    #     '''
    #     Do some preprocessing procedure such as transfering time span format used by ANSYN,L3DAS21 to frame format uesd by STARSS2023.
        
    #     '''
    #     self._label_dir = self.get_label_dir()
    #     create_folder(self._label_dir)
    #     if self._dataset in ['ANSYN', 'ASIRD', 'L3DAS21']:
    #         for sub_folder in os.listdir(self._dataset_dir):
    #             if 'desc' not in sub_folder: 
    #                 continue 
    #             modified_subfolder = os.path.join(self._label_dir, sub_folder)
    #             create_folder(modified_subfolder)
    #             for file_cnt, file_name in enumerate(os.listdir(sub_folder)):  
    #                 pass 





    # ------------------------------- EXTRACT VISUAL FEATURES AND PREPROCESS IT -------------------------------

    @staticmethod 
    def _read_vid_frames(vid_filename):
        cap = cv2.VideoCapture(vid_filename)
        frames = []
        frame_cnt = 0
        while True:
            ret, frame = cap.read()   # ret: bool, frame: ndarray 960, 1920, 3 height width channel
            if not ret:
                break
            if frame_cnt % 3 == 0:   # every 3 frame 
                resized_frame = cv2.resize(frame, (360, 180))   # resize image to 180, 360, 3
                frame_rgb = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGB)  # BGR2RGB
                # pil_frame = Image.fromarray(frame_rgb) # ndarray to PIL 
                frames.append(frame_rgb) # 
            frame_cnt += 1
        cap.release()
        cv2.destroyAllWindows()
        
        frames_array = np.array(frames)  # Convert to numpy array of shape [N, H, W, C]

        return frames_array.transpose(0, 3, 1, 2) # Reorder to [N, C, H, W]

    def extract_file_vid_feature(self, _arg_in):
        _file_cnt, _mp4_path, _vid_feat_path = _arg_in
        vid_feat = None
        before_read_frame = time.time()
        # 
        vid_frames = self._read_vid_frames(_mp4_path) # len(vid_frames):T/5, vid_frames[i]{ image mode=RGB size=360x180}
        print(f'\t\t time for read vid frame = {time.time() - before_read_frame}')
        before_model_inference = time.time()
        vid_frames_tensor = torch.tensor(vid_frames).float().to(self.device)
        vid_feat = self.pretrained_vid_model(vid_frames_tensor)    # tensor shape   T/5, 7, 7
        vid_feat = vid_feat.cpu().numpy() if self.device == 'cuda' else vid_feat.numpy()
        print(f'\t\t time for model inference = {time.time() - before_model_inference}')
        vid_feat = np.array(vid_feat) 

        if vid_feat is not None:
            print('{}: {}, {}'.format(_file_cnt, os.path.basename(_mp4_path), vid_feat.shape))
            np.save(_vid_feat_path, vid_feat)  # feat_label_hnet/video_dev/_vid_feat_path  -> 

    def extract_visual_features(self):
        self._vid_feat_dir = self.get_vid_feat_dir()
        create_folder(self._vid_feat_dir)
        print('Extracting visual features:')
        print('\t\t vid_dir {} \n\t\t vid_feat_dir {}'.format(
            self._vid_dir, self._vid_feat_dir))
        for sub_folder in os.listdir(self._vid_dir): # self.vid_dir '../Dataset/STARSS2023\\video_dev'
            loc_vid_folder = os.path.join(self._vid_dir, sub_folder)
            for file_cnt, file_name in enumerate(os.listdir(loc_vid_folder)):
                print(file_name) # fold4_room23_mix001.mp4
                mp4_filename = '{}.mp4'.format(file_name.split('.')[0])
                mp4_path = os.path.join(loc_vid_folder, mp4_filename) # '../Dataset/STARSS2023\\video_dev\\dev-test-sony\\fold4_room23_mix001.mp4'
                vid_feat_path = os.path.join(self._vid_feat_dir, '{}.npy'.format(mp4_filename.split('.')[0])) # '../Dataset/STARSS2023/feat_label_hnet/video_dev\\fold4_room23_mix001.npy'
                # Check if the feature file already exists to avoid reprocessing
                if not os.path.exists(vid_feat_path):
                    self.extract_file_vid_feature((file_cnt, mp4_path, vid_feat_path))
                else:
                    print(f"Skipping {mp4_filename} as features are already extracted.")

    # -------------------------------  DCASE OUTPUT  FORMAT FUNCTIONS -------------------------------
    def generate_standard_label_df(self, loc_desc_folder, file_name):
        old_csv_file = os.path.join(loc_desc_folder,file_name)

        frame_expansion_factor = int(self._label_hop_len_s_STARSS// self._label_hop_len_s)

        if self._dataset in ['STARSS2023']:
            df = pd.read_csv(old_csv_file, header=None)
            expanded_rows = []
            for _, row in df.iterrows():
                frame = int(row[0])
                new_class_idx = int(row[1])
                for i in range(frame_expansion_factor):
                    new_row = row.copy()
                    new_row[0] = frame * frame_expansion_factor + i -1
                    new_row[1] = new_class_idx
                    
                    expanded_rows.append(new_row)
            expanded_rows = pd.DataFrame(expanded_rows)

            expanded_rows[0] = expanded_rows[0].astype(int)
            expanded_rows[1] = expanded_rows[1].astype(int)
            expanded_rows[2] = expanded_rows[2].astype(int)
            expanded_rows[3] = expanded_rows[3].astype(float)
            expanded_rows[4] = expanded_rows[4].astype(float)
            expanded_rows[5] = expanded_rows[5].astype(float)
        elif self._dataset in ['ANSYN', 'ASIRD']:
            
            column_names = ['sound_event_recording', 'start_time', 'end_time', 'ele', 'azi', 'dis']
            df = pd.read_csv(old_csv_file, header=0, names=column_names)
            expanded_rows = []
            for _, row in df.iterrows():
                start_time = row['start_time']
                end_time = row['end_time']
                ele = row['ele']
                azi = row['azi']
                dis = row['dis'] * 100  # convert m to cm if not already cm
                
                start_frame = int(start_time // self._label_hop_len_s)
                end_frame = int(end_time // self._label_hop_len_s)
                
                class_idx = self.get_class_index(row['sound_event_recording'])
                
                for frame_idx in range(start_frame, end_frame + 1):
                    
                    
                    # Here you would call your class_idx and source_id functions, which are not specified
                    source_id = 0   # Assume all the source id are 0
                    expanded_rows.append([frame_idx, class_idx, source_id, azi, ele, dis])
            
            expanded_rows = pd.DataFrame(expanded_rows)
        elif self._dataset in ['L3DAS21']:
            column_names = ['File', 'Start', 'End', 'Class', 'X', 'Y', 'Z']
            df = pd.read_csv(old_csv_file, header=0, names=column_names)
            expanded_rows = []
            for _, row in df.iterrows():
                x, y, z = row['X'], row['Y'], row['Z']

                    # in degrees
                azi = np.arctan2(y, x) * 180 / np.pi
                ele = np.arctan2(z, np.sqrt(x**2 + y**2)) * 180 / np.pi

                dis = np.sqrt(x**2 + y**2 + z**2) * 100
                start_time = row['Start']
                end_time = row['End']
                dis = dis  # convert m to cm if not already cm
                
                start_frame = int(start_time // self._label_hop_len_s)
                end_frame = int(end_time // self._label_hop_len_s)
                
                class_idx = self.get_class_index(row['Class'])
                
                for frame_idx in range(start_frame, end_frame + 1):
                    
                    
                    # Here you would call your class_idx and source_id functions, which are not specified
                    source_id = 0   # Assume all the source id are 0
                    expanded_rows.append([frame_idx, class_idx, source_id, azi, ele, dis])
            
            expanded_rows = pd.DataFrame(expanded_rows)
        
        return expanded_rows    

    def load_output_format_file(self, _output_format_file, cm2m=False):
        """
        Loads DCASE output format csv file and returns it in dictionary format

        :param _output_format_file: DCASE output format CSV
        :return: _output_dict: dictionary
        """
        _output_dict = {}
        try:
            _fid = open(_output_format_file, 'r')
            _words = []  # For empty files

            for _line in _fid:
                try:
                    _words = _line.strip().split(',')
                    _frame_ind = int(_words[0])  # Convert frame index to int
                    
                    if _frame_ind not in _output_dict:
                        _output_dict[_frame_ind] = []
                    
                    if len(_words) == 4:  # frame, class idx, polar coordinates(2)
                        _output_dict[_frame_ind].append([
                            int(_words[1]), 0, float(_words[2]), float(_words[3])
                        ])
                    elif len(_words) == 5:  # frame, class idx, source_id, polar coordinates(2)
                        _output_dict[_frame_ind].append([
                            int(_words[1]), int(_words[2]), float(_words[3]), float(_words[4])
                        ])
                    elif len(_words) == 6:  # frame, class idx, source_id, polar coordinates(2), distance
                        _output_dict[_frame_ind].append([
                            int(_words[1]), int(_words[2]), float(_words[3]), float(_words[4]),
                            float(_words[5])/100 if cm2m else float(_words[5])
                        ])
                    elif len(_words) == 7:  # frame, class idx, source_id, cartesian coordinates(3), distance
                        _output_dict[_frame_ind].append([
                            int(_words[1]), int(_words[2]), float(_words[3]), float(_words[4]), 
                            float(_words[5]), float(_words[6])/100 if cm2m else float(_words[6])
                        ])
                except ValueError as e:
                    print(f"Error processing line: {_line.strip()} in file {_output_format_file}")
                    print(f"Details: {e}")
                    continue  # Skip the current line and move to the next one
            
        except Exception as e:
            print(f"Error opening or reading file {_output_format_file}: {e}")
        finally:
            _fid.close()
        
        if len(_words) == 7:
            _output_dict = self.convert_output_format_cartesian_to_polar(_output_dict)
        
        return _output_dict


    def load_output_format_file_transformation(self, _output_format_file, transformation_idx,cm2m=False):  # TODO: Reconsider cm2m conversion
        """
        Loads DCASE output format csv file and then process channel swapping according the transformation index, and returns it in dictionary format finnaly

        :param _output_format_file: DCASE output format CSV
        :return: _output_dict: dictionary
        """
        _output_dict = {}
        _fid = open(_output_format_file, 'r')
        # next(_fid)
        _words = []     # For empty files
        for _line in _fid:
            _words = _line.strip().split(',')
            _frame_ind = int(_words[0])
            if _frame_ind not in _output_dict:
                _output_dict[_frame_ind] = []
            if len(_words) == 6: # frame, class idx, source_id, polar coordinates(2), distance
                azi, ele = float(_words[3]), float(_words[4])
                azi, ele = self.adjust_angles(azi, ele, transformation_idx)
                _output_dict[_frame_ind].append([int(_words[1]), int(_words[2]), azi, ele, float(_words[5])/100 if cm2m else float(_words[5])])
                #
            else:
                print(f'The length of {len(_words)} for output dict is not supported.')
                sys.exit()
        _fid.close()
        return _output_dict
    

    def adjust_angles(self, azi, ele, transformation_idx):
        '''
        Adjust angles according to transformation index
        '''
        # Normalize azimuth to [-180, 180]
        def normalize_azi(angle):
            if angle >= 180:
                angle = -(360 - angle)
            elif angle <= -180:
                angle %= 360
            return angle
        
        if transformation_idx == 1:  # Rotate azi by -90°, Y->X, -X->Y
            azi -= 90
            azi = normalize_azi(azi)
            
        elif transformation_idx == 2:  # Original, no changes
            pass
            
        elif transformation_idx == 3:  # Rotate azi by +90°, X->Y, -Y->X
            azi += 90
            azi = normalize_azi(azi)
        
        elif transformation_idx == 4:  # Rotate azi by +180°, -X -> X, -Y->Y
            azi += 180
            azi = normalize_azi(azi)
            
        elif transformation_idx == 5:  # Rotate azi by -90°, flip ele, Y->X, -X->Y, -Z->Z
            azi -= 90
            azi = normalize_azi(azi)
            ele = -ele
            
            
        elif transformation_idx == 6:  # Azi remains unchanged, flip ele: -Z->Z
            ele = -ele
            
            
        elif transformation_idx == 7:  # Rotate azi by +90° and flip ele:-Y->X, X->Y,-Z->Z
            azi += 90
            azi = normalize_azi(azi)
            ele = -ele
            
            
        elif transformation_idx == 8:  # Rotate azi by +180° and flip ele:-X->X, -Y->Y, -Z->Z
            azi += 180
            azi = normalize_azi(azi)
            ele = -ele
            
            
        elif transformation_idx == 9:  # Rotate -azi by -90°: Y->X, -X->Y
            azi = -azi - 90
            azi = normalize_azi(azi)
            
        elif transformation_idx == 10:  # Rotate -azi: -Y -> Y
            azi = -azi
            
        elif transformation_idx == 11:  # Rotate -azi by +90°:Y->X, X->Y
            azi = -azi + 90
            azi = normalize_azi(azi)
            
        elif transformation_idx == 12:  # Rotate -azi by +180°:-X->X
            azi = -azi + 180
            azi = normalize_azi(azi)
            
        elif transformation_idx == 13:  # Rotate -azi by -90° and flip ele:Y->X,-X->Y
            azi = -azi - 90
            azi = normalize_azi(azi)
            ele = -ele
            
            
        elif transformation_idx == 14:  # Rotate -azi and flip ele:-Y->Y, -Z->Z
            azi = -azi
            ele = -ele
            
            
        elif transformation_idx == 15:  # Rotate -azi by +90°, flip ele:Y->X, X->Y, -Z->Z
            azi = -azi + 90
            azi = normalize_azi(azi)
            ele = -ele
            
            
        elif transformation_idx == 16:  # Rotate -azi by 180° and flip ele:-X->X, -Z->Z
            azi = -azi + 180
            azi = normalize_azi(azi)
            ele = -ele
            
        return azi, ele

    def load_output_format_file_from_original_file(self, _output_format_file, cm2m=False):  # TODO: Reconsider cm2m conversion
        """
        Loads DCASE output format csv file and returns it in dictionary format
        For instance, the output format of DCASE 2024 is : 
            [frame number (int)], [active class index (int)], [source number index (int)], [azimuth (int)], [elevation (int)], [distance (int)]
        After processing according to frame hop length, the ouput is:

        :param _output_format_file: DCASE output format CSV
        :return: _output_dict: dictionary
        """
        _output_dict = {}    # {1: [[8, 0, 14.0, 0.0, 392.0], [5, 0, -37.0, -18.0, 205.0]], 
        _fid = open(_output_format_file, 'r')
        # next(_fid)
        _words = []     # For empty files
        
        if self._dataset in ['STARSS2023']:
            if self._label_hop_len_s == self._label_hop_len_s_STARSS: # Donnnot to change the resolution 
                for _line in _fid:
                    _words = _line.strip().split(',')   #'1,8,0,14,0,392\n'
                    _frame_ind = int(_words[0])    # 1
                    if _frame_ind not in _output_dict: 
                        _output_dict[_frame_ind] = []

                    if len(_words) == 4:  # frame, class idx,  polar coordinates(2) # no distance data, for example in eval pred
                        _output_dict[_frame_ind].append([int(_words[1]), 0, float(_words[2]), float(_words[3])])
                    if len(_words) == 5:  # frame, class idx, source_id, polar coordinates(2) # no distance data, for example in synthetic data fold 1 and 2
                        _output_dict[_frame_ind].append([int(_words[1]), int(_words[2]), float(_words[3]), float(_words[4])])
                    
                    # In DCASE 2024, len _words == 6 
                    class_idx = self._classes_mapping[int(_words[1])]
                    if len(_words) == 6: # frame, class idx, source_id, polar coordinates(2), distance
                        _output_dict[_frame_ind].append([class_idx, int(_words[2]), float(_words[3]), float(_words[4]), float(_words[5])/100 if cm2m else float(_words[5])])
                    elif len(_words) == 7: # frame, class idx, source_id, cartesian coordinates(3), distance
                        _output_dict[_frame_ind].append([class_idx, int(_words[2]), float(_words[3]), float(_words[4]), float(_words[5]), float(_words[6])/100 if cm2m else float(_words[6])])
            else: # need to change the resolution 
                frame_expansion_factor = int(self._label_hop_len_s_STARSS// self._label_hop_len_s)
                for _line in _fid:
                    _words = _line.strip().split(',')
                    original_frame_ind = int(_words[0])
                    # Calculate how many new frames correspond to one original frame
                    class_idx = int(_words[1])
                    new_start_frame = original_frame_ind * frame_expansion_factor
                    
                    for new_frame_ind in range(new_start_frame , new_start_frame + frame_expansion_factor):
                        if new_frame_ind not in _output_dict:
                            _output_dict[new_frame_ind] = []
                        
                        if len(_words) >= 6:  # Handles both 6 and 7 column scenarios
                            # Convert distance if needed and adjust columns as necessary
                            distance = float(_words[5])/100 if cm2m else float(_words[5])
                            event_data = [class_idx, int(_words[2]), float(_words[3]), float(_words[4]), distance]
                            if len(_words) == 7:
                                event_data.append(float(_words[6])/100 if cm2m else float(_words[6]))
                            _output_dict[new_frame_ind].append(event_data)

        elif self._dataset in ['ANSYN', 'ASIRD', 'L3DAS21']:
            next(_fid)
            reader = csv.reader(_fid)
            for _words in reader:
                start_time = float(_words[1])
                end_time = float(_words[2])
                ele = float(_words[3])
                azi = float(_words[4])
                dis = float(_words[5]) * 100  # convert m to cm if not already cm
                
                start_frame = int(start_time // self._label_hop_len_s)
                end_frame = int(end_time // self._label_hop_len_s)
                
                for frame_idx in range(start_frame, end_frame + 1):
                    if frame_idx not in _output_dict:
                        _output_dict[frame_idx] = []
                    
                    # Here you would call your class_idx and source_id functions, which are not specified
                    class_idx = self.get_class_index(_words[0])  # Placeholder function
                    source_id = 0   # Assume all the source id are 0

                    _output_dict[frame_idx].append([class_idx, source_id, azi, ele, dis])
        
        _fid.close()
        # if len(_words) == 7:
        #     _output_dict = self.convert_output_format_cartesian_to_polar(_output_dict) # WHY ?TODO
    
        return _output_dict   # len(_output_dict) == 606 == label time resolution

    def load_output_format_file_from_prediction_file(self, _output_format_file, cm2m=False):  # TODO: Reconsider cm2m conversion
        """
        Loads DCASE output format csv file and returns it in dictionary format
        For instance, the output format of DCASE 2024 is : 
            [frame number (int)], [active class index (int)], [source number index (int)], [azimuth (int)], [elevation (int)], [distance (int)]
        After processing according to frame hop length, the ouput is:

        :param _output_format_file: DCASE output format CSV
        :return: _output_dict: dictionary
        """
        _output_dict = {}
        _fid = open(_output_format_file, 'r')
        # next(_fid)
        _words = []     # For empty files
        for _line in _fid:
            _words = _line.strip().split(',')
            _frame_ind = int(_words[0])
            if _frame_ind not in _output_dict:
                _output_dict[_frame_ind] = []
            if len(_words) == 4:  # frame, class idx,  polar coordinates(2) # no distance data, for example in eval pred
                _output_dict[_frame_ind].append([int(_words[1]), 0, float(_words[2]), float(_words[3])])
            if len(_words) == 5:  # frame, class idx, source_id, polar coordinates(2) # no distance data, for example in synthetic data fold 1 and 2
                _output_dict[_frame_ind].append([int(_words[1]), int(_words[2]), float(_words[3]), float(_words[4])])
            if len(_words) == 6: # frame, class idx, source_id, polar coordinates(2), distance
                _output_dict[_frame_ind].append([int(_words[1]), int(_words[2]), float(_words[3]), float(_words[4]), float(_words[5])/100 if cm2m else float(_words[5])])
            elif len(_words) == 7: # frame, class idx, source_id, cartesian coordinates(3), distance
                _output_dict[_frame_ind].append([int(_words[1]), int(_words[2]), float(_words[3]), float(_words[4]), float(_words[5]), float(_words[6])/100 if cm2m else float(_words[6])])
        _fid.close()
        if len(_words) == 7:
            _output_dict = self.convert_output_format_cartesian_to_polar(_output_dict)
    
        return _output_dict   # len(_output_dict) == 606 == label time resolution


    def write_output_format_file(self, _output_format_file, _output_format_dict, _output_format):
        """
        Writes DCASE output format csv file, given output format dictionary

        :param _output_format_file:
        :param _output_format_dict:
        :param _output_format: 'multi_accoda', 'single_accdoa', 'polar'
        :return:
        """
        _fid = open(_output_format_file, 'w')
        if _output_format == 'multi_accdoa':
            # _fid.write('{},{},{},{}\n'.format('frame number with 20ms hop (int)', 'class index (int)', 'azimuth angle (int)', 'elevation angle (int)'))
            for _frame_ind in _output_format_dict.keys():
                for _value in _output_format_dict[_frame_ind]:
                    # Write Cartesian format output. Since baseline does not estimate track count and distance we use fixed values.
                    _fid.write('{},{},{},{},{},{},{}\n'.format(int(_frame_ind), int(_value[0]), 0, float(_value[1]), float(_value[2]), float(_value[3]), float(_value[4])))
                    # TODO: What if our system estimates track count and distence (or only one of them)
        elif _output_format == 'single_accoda':
            for _frame_ind in _output_format_dict.keys():
                for _value in _output_format_dict[_frame_ind]:
                    # Write Cartesian format output. Since baseline does not estimate track count and distance we use fixed values.
                    _fid.write('{},{},{},{},{},{},{}\n'.format(int(_frame_ind), int(_value[0]), 0, float(_value[1]), float(_value[2]), float(_value[3]), float(_value[4])))
                    # TODO: What if our system estimates track count and distence (or only one of them)
        elif _output_format == 'polar':
            for _frame_ind in _output_format_dict.keys():
                for _value in _output_format_dict[_frame_ind]:
                    # Write Cartesian format output. Since baseline does not estimate track count and distance we use fixed values.
                    _fid.write('{},{},{},{},{},{},{}\n'.format(int(_frame_ind), int(_value[0]), 0, float(_value[1]), float(_value[2]), float(_value[3]), float(_value[4])))
                    # TODO: What if our system estimates track count and distence (or only one of them)
        elif _output_format == 'dict_to_polar':
            for _frame_ind in _output_format_dict.keys():
                for _value in _output_format_dict[_frame_ind]:
                    # Write Polar format output. Since baseline does not estimate track count we use fixed values. Write new label file based on new label resolution. The type of
                    _fid.write('{},{},{},{},{},{}\n'.format(int(_frame_ind), int(_value[0]), 0, float(_value[2]), float(_value[3]), float(_value[4])))
        _fid.close()

    def segment_labels(self, _pred_dict, _max_frames):
        '''
            Collects class-wise sound event location information in segments of length 1s from reference dataset
        :param _pred_dict: Dictionary containing frame-wise sound event time and location information. Output of SELD method
        :param _max_frames: Total number of frames in the recording
        :return: Dictionary containing class-wise sound event location information in each segment of audio
                dictionary_name[segment-index][class-index] = list(frame-cnt-within-segment, azimuth, elevation)
        '''
        nb_blocks = int(np.ceil(_max_frames / float(self._nb_label_frames_1s)))
        output_dict = {x: {} for x in range(nb_blocks)}
        for frame_cnt in range(0, _max_frames, self._nb_label_frames_1s):

            # Collect class-wise information for each block
            # [class][frame] = <list of doa values>
            # Data structure supports multi-instance occurence of same class
            block_cnt = frame_cnt // self._nb_label_frames_1s
            loc_dict = {}
            for audio_frame in range(frame_cnt, frame_cnt + self._nb_label_frames_1s):
                if audio_frame not in _pred_dict:
                    continue
                for value in _pred_dict[audio_frame]:
                    if value[0] not in loc_dict:
                        loc_dict[value[0]] = {}

                    block_frame = audio_frame - frame_cnt
                    if block_frame not in loc_dict[value[0]]:
                        loc_dict[value[0]][block_frame] = []
                    loc_dict[value[0]][block_frame].append(value[1:])

            # Update the block wise details collected above in a global structure
            for class_cnt in loc_dict:
                if class_cnt not in output_dict[block_cnt]:
                    output_dict[block_cnt][class_cnt] = []

                keys = [k for k in loc_dict[class_cnt]]
                values = [loc_dict[class_cnt][k] for k in loc_dict[class_cnt]]

                output_dict[block_cnt][class_cnt].append([keys, values])

        return output_dict

    def organize_labels(self, _pred_dict, _max_frames):
        '''
            Collects class-wise sound event location information in every frame, similar to segment_labels but at frame level
        :param _pred_dict: Dictionary containing frame-wise sound event time and location information. Output of SELD method
        :param _max_frames: Total number of frames in the recording
        :return: Dictionary containing class-wise sound event location information in each frame
                dictionary_name[frame-index][class-index][track-index] = [azimuth, elevation, (distance)]
        '''
        nb_frames = _max_frames
        output_dict = {x: {} for x in range(nb_frames)}
        for frame_idx in range(0, _max_frames):
            if frame_idx not in _pred_dict:
                continue
            for [class_idx, track_idx, az, el, *dist] in _pred_dict[frame_idx]:  # the pred dict are xyz not ele azi[[8, 0, 0.9702957262759965, 0.24192189559966773, 0.0, 3.92], [5, 0, 0.7595475059751814, -0.5723600993730742, -0.3090169943749474, 2.05]]
                if class_idx not in output_dict[frame_idx]: 
                    output_dict[frame_idx][class_idx] = {}
                # assert track_idx not in output_dict[frame_idx][class_idx]  # I don't know why sometimes this happens... they seem to be repeated DOAs # TODO: Is this still happening?
                output_dict[frame_idx][class_idx][track_idx] = [az, el] + dist
        # 1: {8: {0: [0.9702957262759965, 0.24192189559966773, 0.0, 3.92]}, 5: {0: [0.7595475059751814, -0.5723600993730742, -0.3090169943749474, 2.05]}}
        # frame: {class: {track: [x, y, z, dist]}}
        return output_dict

    def regression_label_format_to_output_format(self, _sed_labels, _doa_labels):
        """
        Converts the sed (classification) and doa labels predicted in regression format to dcase output format.

        :param _sed_labels: SED labels matrix [nb_frames, nb_classes]
        :param _doa_labels: DOA labels matrix [nb_frames, 2*nb_classes] or [nb_frames, 3*nb_classes]
        :return: _output_dict: returns a dict containing dcase output format
        """

        _nb_classes = self._nb_unique_classes
        _is_polar = _doa_labels.shape[-1] == 2*_nb_classes
        _azi_labels, _ele_labels = None, None
        _x, _y, _z = None, None, None
        if _is_polar:
            _azi_labels = _doa_labels[:, :_nb_classes]
            _ele_labels = _doa_labels[:, _nb_classes:]
        else:
            _x = _doa_labels[:, :_nb_classes]
            _y = _doa_labels[:, _nb_classes:2*_nb_classes]
            _z = _doa_labels[:, 2*_nb_classes:]

        _output_dict = {}
        for _frame_ind in range(_sed_labels.shape[0]):
            _tmp_ind = np.where(_sed_labels[_frame_ind, :])
            if len(_tmp_ind[0]):
                _output_dict[_frame_ind] = []
                for _tmp_class in _tmp_ind[0]:
                    if _is_polar:
                        _output_dict[_frame_ind].append([_tmp_class, _azi_labels[_frame_ind, _tmp_class], _ele_labels[_frame_ind, _tmp_class]])
                    else:
                        _output_dict[_frame_ind].append([_tmp_class, _x[_frame_ind, _tmp_class], _y[_frame_ind, _tmp_class], _z[_frame_ind, _tmp_class]])
        return _output_dict

    def convert_output_format_polar_to_cartesian(self, in_dict):
        """
        Convert output format of polar to cartesian.
        For instance, in in_dict the format is
            1: [[8, 0, 14.0, 0.0, 392.0], [5, 0, -37.0, -18.0, 205.0]]
        In out_dict the format is 
            1: [[8, 0, 0.9702957262759965, 0.24192189559966773, 0.0, 392.0], [5, 0, 0.7595475059751814, -0.5723600993730742, -0.3090169943749474, 205.0]]

        :param in_dict: dictionary whose keys are time frame and values are events properties
        :return :dictionary whose keys are time frame and values are events properties, but in cartesian format
        """
        out_dict = {}  
        for frame_cnt in in_dict.keys():
            if frame_cnt not in out_dict:
                out_dict[frame_cnt] = []
                for tmp_val in in_dict[frame_cnt]:
                    ele_rad = tmp_val[3]*np.pi/180.
                    azi_rad = tmp_val[2]*np.pi/180.
                    # 
                    tmp_label = np.cos(ele_rad)
                    x = np.cos(azi_rad) * tmp_label
                    y = np.sin(azi_rad) * tmp_label
                    z = np.sin(ele_rad)
                    out_dict[frame_cnt].append(tmp_val[0:2] + [x, y, z] + tmp_val[4:])
        return out_dict
    
    def convert_output_format_polar_to_cartesian(self, in_dict):
        """
        Convert output format of polar to cartesian.
        For instance, in in_dict the format is
            1: [[8, 0, 14.0, 0.0, 392.0], [5, 0, -37.0, -18.0, 205.0]]
        In out_dict the format is 
            1: [[8, 0, 0.9702957262759965, 0.24192189559966773, 0.0, 392.0], [5, 0, 0.7595475059751814, -0.5723600993730742, -0.3090169943749474, 205.0]]

        :param in_dict: dictionary whose keys are time frame and values are events properties
        :return :dictionary whose keys are time frame and values are events properties, but in cartesian format
        """
        out_dict = {}  
        for frame_cnt in in_dict.keys():
            if frame_cnt not in out_dict:
                out_dict[frame_cnt] = []
                for tmp_val in in_dict[frame_cnt]:
                    ele_rad = tmp_val[3]*np.pi/180.
                    azi_rad = tmp_val[2]*np.pi/180.
                    # 
                    tmp_label = np.cos(ele_rad)
                    x = np.cos(azi_rad) * tmp_label
                    y = np.sin(azi_rad) * tmp_label
                    z = np.sin(ele_rad)
                    out_dict[frame_cnt].append(tmp_val[0:2] + [x, y, z] + tmp_val[4:])
        return out_dict
    
    def convert_output_format_cartesian_to_polar(self, in_dict):
        out_dict = {}
        for frame_cnt in in_dict.keys():
            if frame_cnt not in out_dict:
                out_dict[frame_cnt] = []
                for tmp_val in in_dict[frame_cnt]:
                    x, y, z = tmp_val[2], tmp_val[3], tmp_val[4]

                    # in degrees
                    azimuth = np.arctan2(y, x) * 180 / np.pi
                    elevation = np.arctan2(z, np.sqrt(x**2 + y**2)) * 180 / np.pi
                    r = np.sqrt(x**2 + y**2 + z**2)
                    out_dict[frame_cnt].append(tmp_val[0:2] + [azimuth, elevation] + tmp_val[5:])
        return out_dict
    # ------------------------------- Class mapping functions -------------------------------
    def get_class_index(self, original_sound_class):
        '''

        '''
        if self._dataset in ['STARSS2023', 'L3DAS21']:
            return self._classes_mapping[original_sound_class]
        elif self._dataset in ['ANSYN', 'ASIRD']:
            original_sound_class = ''.join(filter(str.isalpha, original_sound_class.split('.')[0]))  # clearthroat094.wav -> clearthroat
            return self._classes_mapping[original_sound_class]



    # ------------------------------- Misc public functions -------------------------------

    def get_normalized_feat_dir(self):
        return os.path.join(
            self._feat_label_dir,
            "{}_{}_{}bins_{}_{}norm{}".format(
                '{}_salsa'.format(self._dataset_combination) if (self._dataset=='mic' and self._use_salsalite) else self._dataset_combination, 
                self._filter_type,
                self._nb_mel_bins,
                self._preprocessing_type,
                'dataaug_' if self._data_augmentation is True else '',
                '_subset' if self._subset is True else ''),
        )

    def get_sampled_feat_dir_norm(self):
        return '/lab/chengr_lab/12232381/dataset/STARSS2023/feat_label_hnet/foa_dev_gammatone_128bins_iv_7_norm/A_subset_files.pkl'
    
    def get_unnormalized_feat_dir(self):
        
        return os.path.join(
            self._feat_label_dir,
            "{}_{}_{}bins_{}".format(
                '{}_salsa'.format(self._dataset_combination) if (self._dataset=='mic' and self._use_salsalite) else self._dataset_combination, 
                self._filter_type,
                self._nb_mel_bins,
                self._preprocessing_type)
        )

    def get_label_dir(self):
        if self._is_eval:
            return None
        else:
            return os.path.join(
                self._feat_label_dir,
                f"{self._dataset_combination}_{self._output_format}_{int(self._label_hop_len_s*1000)}msres_label_without_classes_modifying{('_' + self._filter_type) if self._data_augmentation is True else ''}"              
        )
        # f"{self._dataset_combination}_{self._output_format}_{int(self._label_hop_len_s*1000)}msres_label_without_classes_modifying_{'data_augmentation' if self._data_augmentation is True else 'without_data_augmentation'}_{self._filter_type}"              

    def get_new_label_dir(self):

        return os.path.join(
            self._dataset_dir,
            f"{int(self._label_hop_len_s * 1000)}ms_labels_csv_without_classes_modifying_{self._filter_type}"               
        )

    def get_normalized_wts_file(self):
        return os.path.join(
            self.get_normalized_feat_dir(), 
            '{}_{}wts'.format(self._data_type, self._filter_type)
        )

    def get_vid_feat_dir(self):
        return os.path.join(self._feat_label_dir, 'video_{}'.format('eval' if self._is_eval else 'dev'))

    def get_nb_channels(self):
        return self._nb_channels

    def get_nb_classes(self):
        return self._nb_unique_classes

    def nb_frames_1s(self):
        return self._nb_label_frames_1s

    def get_hop_len_sec(self):
        return self._hop_len_s

    def get_nb_mel_bins(self):
        return self._nb_mel_bins


def create_folder(folder_name):

    if not os.path.exists(folder_name):
        print('{} folder does not exist, creating it.'.format(folder_name))
        os.makedirs(folder_name)
    else: 
        print(f'{folder_name} do exist, so skep creating it')


def delete_and_create_folder(folder_name):
    if os.path.exists(folder_name) and os.path.isdir(folder_name):
        shutil.rmtree(folder_name)
    os.makedirs(folder_name, exist_ok=True)


def main(argv):
    # Expects one input - task-id - corresponding to the configuration given in the parameter.py file.
    # Extracts features and labels relevant for the task-id
    # It is enough to compute the feature and labels once. 

    # use parameter set defined by user
    task_id = '1' if len(argv) < 2 else argv[1]
    params = parameters.get_params(task_id)

    # ------------- Extract features and labels for development set -----------------------------
    dev_feat_cls = FeatureClass(params)
    dev_feat_cls.get_frame_stats()
    # # # Extract labels
    # dev_feat_cls.generate_new_labels()  
    # dev_feat_cls.extract_all_labels()
    
    # breakpoint()
    # # Extract features and normalize them
    # breakpoint()
    # dev_feat_cls.extract_all_features_and_labels()
    # dev_feat_cls.extract_all_feature_augmentation()
    # dev_feat_cls.preprocess_features()


    # # Extract visual features
    # if params['modality'] == 'audio_visual':
    #     dev_feat_cls.extract_visual_features()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (ValueError, IOError) as e:
        sys.exit(e)

