#!/usr/bin/env python
'''
CREATED:2012-11-05 14:38:03 by Brian McFee <brm2132@columbia.edu>

All things rhythmic go here

- Onset detection
- Tempo estimation
- Beat tracking
- Segmentation

'''

import librosa
import numpy, scipy, scipy.signal, scipy.ndimage
import sklearn, sklearn.cluster, sklearn.feature_extraction

def beat_track(onsets=None, y=None, sr=22050, hop_length=64, start_bpm=120.0):
    '''
    Ellis-style beat tracker

    Input:
        onsets:         pre-computed onset envelope                 | default: None
        y:              time-series data                            | default: None
        sr:             sample rate of y                            | default: 22050
        hop_length:     hop length (in frames) for onset detection  | default: 64
        start_bpm:      initial guess for BPM estimator             | default: 120.0

        Either onsets or y must be provided.

    Output:
        bpm:            estimated global tempo
        beats:          array of estimated beats by frame number
    '''

    # First, get the frame->beat strength profile if we don't already have one
    if onsets is None:
        if y is None:
            raise ValueError('Either "y" or "onsets" must be provided')

        onsets  = onset_strength(y=y, sr=sr, hop_length=hop_length)

    # Then, estimate bpm
    bpm     = onset_estimate_bpm(onsets, start_bpm, sr, hop_length)
    
    # Then, run the tracker: tightness = 400
    beats   = _beat_tracker(onsets, bpm, sr, hop_length, 400)

    return (bpm, beats)



def _beat_tracker(onsets, start_bpm, sr, hop_length, tightness):
    '''
        Internal function that does beat tracking from a given onset profile.

    '''
    fft_res     = numpy.float(sr) / hop_length
    period      = round(60.0 * fft_res / start_bpm)

    # AGC the onset envelope
    onsets      = onsets / onsets.std(ddof=1)

    # Smooth beat events with a gaussian window
    # FIXME:  2013-03-23 09:31:04 by Brian McFee <brm2132@columbia.edu>
    # this is uglified to match matlab implementation     
    template    = numpy.exp(-0.5 * ((numpy.arange(-period, period+1) * 32.0 / period )**2))

    # Convolve 
    localscore  = scipy.signal.convolve(onsets, template, 'same')
    max_score   = numpy.max(localscore)

    ### Initialize DP

    backlink    = numpy.zeros_like(localscore, dtype=int)
    cumscore    = numpy.zeros_like(localscore)

    # Search range for previous beat: number of samples forward/backward to look
    window      = numpy.arange(-2*period, -numpy.round(period/2) + 1, dtype=int)

    # Make a score window, which begins biased toward start_bpm and skewed 
    txwt        = - tightness * numpy.abs(numpy.log(-window /period))**2

    # Are we on the first beat?
    first_beat      = True

    time_range      = window
    # Forward step
    for i in xrange(len(localscore)):

        # Are we reaching back before time 0?
        z_pad = numpy.maximum(0, min(- time_range[0], len(window)))

        # Search over all possible predecessors and apply transition weighting
        score_candidates                = txwt.copy()
        score_candidates[z_pad:]        = score_candidates[z_pad:] \
                                        + cumscore[time_range[z_pad:]]

        # Find the best predecessor beat
        beat_location       = numpy.argmax(score_candidates)
        current_score       = score_candidates[beat_location]

        # Add the local score
        cumscore[i]         = current_score + localscore[i]

        # Special case the first onset.  Stop if the localscore is small
        if first_beat and localscore[i] < 0.01 * max_score:
            backlink[i]     = -1
        else:
            backlink[i]     = time_range[beat_location]
            first_beat      = False

        # Update the time range
        time_range          = time_range + 1

    ### Get the last beat
    maxes           = librosa.localmax(cumscore)
    max_indices     = maxes.nonzero()[0]
    peak_scores     = cumscore[max_indices]

    median_score    = numpy.median(peak_scores)
    bestendposs     = (cumscore * maxes * 2 > median_score).nonzero()[0]

    # The last of these is the last beat (since score generally increases)
    b               = [int(bestendposs.max())]

    while backlink[b[-1]] >= 0:
        b.append(backlink[b[-1]])

    # Put the beats in ascending order
    b.reverse()

    # Convert into an array of frame numbers
    b = numpy.array(b, dtype=int)

    # Final post-processing: throw out spurious leading/trailing beats
    boe             = localscore[b]
    smooth_boe      = scipy.signal.convolve(boe, scipy.signal.hann(5), 'same')

    threshold       = 0.5 * ((smooth_boe**2).mean()**0.5)

    valid           = numpy.argwhere(smooth_boe > threshold)
    b               = b[valid.min():valid.max()]

    # Add one to account for differencing offset
    return 1 + b

def onset_estimate_bpm(onsets, start_bpm, sr, hop_length):
    '''
    Estimate the BPM from an onset envelope.

    Input:
        onsets:         time-series of onset strengths
        start_bpm:      initial guess of the BPM
        sr:             sample rate of the time series
        hop_length:     hop length of the time series

    Output:
        estimated BPM
    '''
    AC_SIZE     = 4.0
    DURATION    = 90.0
    END_TIME    = 90.0
    BPM_STD     = 1.0

    fft_res     = numpy.float(sr) / hop_length

    # Chop onsets to X[(upper_limit - duration):upper_limit]
    # or as much as will fit
    maxcol      = min(len(onsets)-1, numpy.round(END_TIME * fft_res))
    mincol      = max(0,    maxcol - numpy.round(DURATION * fft_res))

    # Use auto-correlation out of 4 seconds (empirically set??)
    ac_window   = numpy.round(AC_SIZE * fft_res)

    # Compute the autocorrelation
    x_corr      = librosa.autocorrelate(onsets[mincol:maxcol], ac_window)


    #   FIXME:  2013-01-25 08:55:40 by Brian McFee <brm2132@columbia.edu>
    #   this fails if ac_window > length of song   
    # re-weight the autocorrelation by log-normal prior
    bpms    = 60.0 * fft_res / (numpy.arange(1, ac_window+1))

    # Smooth the autocorrelation by a log-normal distribution
    x_corr  = x_corr * numpy.exp(-0.5 * ((numpy.log2(bpms / start_bpm)) / BPM_STD)**2)

    # Get the local maximum of weighted correlation
    x_peaks = librosa.localmax(x_corr)

    # Zero out all peaks before the first negative
    x_peaks[:numpy.argmax(x_corr < 0)] = False

    # Find the largest (local) max
    start_period    = numpy.argmax(x_peaks * x_corr)

    # Choose the best peak out of .33, .5, 2, 3 * start_period
    candidates      = numpy.multiply(start_period, [1.0/3, 1.0/2, 1.0, 2.0, 3.0])
    candidates      = candidates.astype(int)
    candidates      = candidates[candidates < ac_window]

    best_period     = numpy.argmax(x_corr[candidates])

    return 60.0 * fft_res / candidates[best_period]


def onset_strength(S=None, y=None, sr=22050, **kwargs):
    '''
    Adapted from McVicar, adapted from Ellis, etc...
    
    Extract onsets

    INPUT:
        S               = pre-computed spectrogram              | default: None
        y               = time-series waveform (t-by-1 vector)  | default: None
        sr              = sampling rate of the input signal     | default: 22050

        Either S or y,sr must be provided.

        **kwargs        = Parameters to mel spectrogram, if S is not provided
                          See librosa.feature.melspectrogram() for details

    OUTPUT:
        onset_envelope
    '''

    # First, compute mel spectrogram
    if S is None:
        if y is None:
            raise ValueError('One of "S" or "y" must be provided.')

        S   = librosa.feature.melspectrogram(y, sr = sr, **kwargs)

        # Convert to dBs
        S   = librosa.logamplitude(S)


    ### Compute first difference
    onsets  = numpy.diff(S, n=1, axis=1)

    ### Discard negatives (decreasing amplitude)
    #   falling edges could also be useful segmentation cues
    #   to catch falling edges, replace max(0,D) with abs(D)
    onsets  = numpy.maximum(0.0, onsets)

    ### Average over mel bands
    onsets  = onsets.mean(axis=0)

    ### remove the DC component
    onsets  = scipy.signal.lfilter([1.0, -1.0], [1.0, -0.99], onsets)

    return onsets 

def segment(X, k):
    '''
        Perform bottom-up temporal segmentation

        Input:
            X:  d-by-t  spectrogram (t frames)
            k:          number of segments to produce

        Output:
            s:          segment boundaries (frame numbers)
            centroid:   d-by-k  centroids (ordered temporall)
            variance:   d-by-k  variance (mean distortion) for each segment

    '''

    # Connect the temporal connectivity graph
    G = sklearn.feature_extraction.image.grid_to_graph( n_x=X.shape[1], 
                                                        n_y=1, 
                                                        n_z=1)

    # Instantiate the clustering object
    W = sklearn.cluster.Ward(n_clusters=k, connectivity=G)

    # Fit the model
    W.fit(X.T)

    # Instantiate output objects
    C = numpy.zeros( (X.shape[0], k) )
    V = numpy.zeros( (X.shape[0], k) )
    N = numpy.zeros(k, dtype=int)

    # Find the change points from the labels
    d = list(1 + numpy.nonzero(numpy.diff(W.labels_))[0].astype(int))

    # tack on the last frame as a change point
    d.append(X.shape[1])

    s = 0
    for (i, t) in enumerate(d):
        N[i]    = s
        C[:, i] = numpy.mean(X[:, s:t], axis=1)
        V[:, i] = numpy.var(X[:, s:t], axis=1)
        s       = t

    return (N, C, V)
