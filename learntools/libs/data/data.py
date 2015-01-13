import gzip
import cPickle
from itertools import count, izip, groupby

import numpy


class DataSet:
    def __init__(self):
        self.skills = numpy.array([0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1])
        self.cond = numpy.array([0, 0, 1, 1, 1, 1, 0, 0, 0, 1, 1])


def gen_data(fname):
    inp = numpy.array([[0], [0], [0], [1], [1], [1]])
    target = numpy.array([1, 1, 1, 0, 0, 0])
    set_ = (inp, target)
    with gzip.open(fname, 'w') as f:
        cPickle.dump((set_, set_, set_), f)


def convert_task_from_xls(fname, outname=None):
    from io import load, Dataset
    headers = (('cond', Dataset.INT),
               ('subject', Dataset.ENUM),
               ('stim', Dataset.ENUM),
               ('block', Dataset.ENUM),
               ('start_time', Dataset.TIME),
               ('end_time', Dataset.TIME))
    data = load('learntools/libs/data/tests/sample_data.xls', headers)

    subject = data.get_data('subject')
    correct = data.get_data('cond')
    skill = data.get_data('stim')
    start_time = data.get_data('start_time')
    end_time = data.get_data('end_time')
    stim_pairs = data.get_column('stim').enum_pairs
    subject_pairs = data.get_column('subject').enum_pairs
    formatted_data = (subject, start_time, end_time, skill, correct, subject_pairs, stim_pairs)
    if outname is not None:
        with gzip.open(outname, 'w') as f:
            cPickle.dump(formatted_data, f)
    else:
        return formatted_data


def convert_eeg_from_xls(fname, outname=None, cutoffs=(0.5, 4.0, 7.0, 12.0, 30.0)):
    from io import load, Dataset
    from eeg import signal_to_freq_bins
    headers = (('sigqual', Dataset.INT),
               ('subject', Dataset.ENUM),
               ('start_time', Dataset.TIME),
               ('end_time', Dataset.TIME),
               ('rawwave', Dataset.STR))
    data = load(fname, headers)
    subject_pairs = data.get_column('subject').enum_pairs
    subject = data.get_data('subject')
    sigqual = data.get_data('sigqual')
    start_time = data.get_data('start_time')
    end_time = data.get_data('end_time')
    rawwave = data.get_data('rawwave')
    cutoffs = list(cutoffs)
    eeg_freq = numpy.empty((len(rawwave), len(cutoffs) - 1))
    for i, eeg_str in enumerate(rawwave):
        eeg = [float(d) for d in eeg_str.strip().split(' ')]
        eeg_freq[i] = tuple(signal_to_freq_bins(eeg, cutoffs=cutoffs, sampling_rate=512))
    formatted_data = (subject, start_time, end_time, sigqual, eeg_freq, subject_pairs)
    if outname is not None:
        with gzip.open(outname, 'w') as f:
            cPickle.dump(formatted_data, f)
    return formatted_data


def align_data(task_name, eeg_name, out_name=None, sigqual_cutoff=200):
    with gzip.open(task_name, 'rb') as task_f, gzip.open(eeg_name, 'rb') as eeg_f:
        task_subject, task_start, task_end, skill, correct, task_subject_pairs, stim_pairs = cPickle.load(task_f)
        eeg_subject, eeg_start, eeg_end, sigqual, eeg_freq, eeg_subject_pairs = cPickle.load(eeg_f)
    eeg_freq = numpy.asarray(eeg_freq, dtype='float32')
    num_tasks = len(task_start)

    # Step1: convert to dictionary with subject_id as keys and rows sorted by
    # start_time as values
    def convert_format(subject, start, *rest, **kwargs):
        data_sorted = sorted(izip(subject, start, *rest), key=lambda v: v[:2])
        data_by_subject = {k: list(v) for k, v in groupby(data_sorted, lambda v: v[0])}
        if 'subject_pairs' in kwargs:
            subject_dict = {k: v for v, k in kwargs['subject_pairs']}
            data_by_subject = {subject_dict[k]: v for k, v in data_by_subject.iteritems()}
        return data_by_subject
    task_by_subject = convert_format(task_subject, task_start, task_end, count(0),
                                     subject_pairs=task_subject_pairs)
    eeg_by_subject = convert_format(eeg_subject, eeg_start, eeg_end, count(0),
                                    subject_pairs=eeg_subject_pairs)

    # Step2: efficiently create mapping between task and eeg using the structured data
    task_eeg_mapping = [None] * num_tasks
    for sub, task in task_by_subject.iteritems():
        if sub not in eeg_by_subject:
            continue
        eeg = eeg_by_subject[sub]
        num_sub_eegs = len(eeg)
        eeg_pointer = 0
        try:
            for t in task:
                _, t_start, t_end, t_i = t
                # throw away eeg before the current task
                # this works because the tasks are sorted by start_time
                while eeg_pointer < num_sub_eegs and eeg[eeg_pointer][2] < t_start:
                    eeg_pointer += 1
                    if eeg_pointer > num_sub_eegs:
                        raise StopIteration
                # map eeg onto the current task
                temp_pointer = eeg_pointer
                task_eeg = []
                # TODO: refactor this while loop into a itertools.takewhile
                while temp_pointer < num_sub_eegs and eeg[temp_pointer][1] < t_end:
                    eeg_i = eeg[temp_pointer][3]
                    if sigqual[eeg_i] < sigqual_cutoff:
                        task_eeg.append(eeg_i)
                    temp_pointer += 1
                if task_eeg:
                    task_eeg_mapping[t_i] = task_eeg
                print task_eeg
        except StopIteration:
            pass

    # Step3: compute eeg features for each task based on aligned eeg
    def compute_eeg_features(eeg_idxs):
        if eeg_idxs is None:
            return None
        return numpy.mean(eeg_freq[eeg_idxs], axis=0)
    features = [compute_eeg_features(ei) if ei else None for ei in task_eeg_mapping]

    # Step4: write data file for use by classifier
    formatted_data = (task_subject, skill, correct, task_start, features, stim_pairs)
    if out_name is not None:
        with gzip.open(out_name, 'w') as f:
            cPickle.dump(formatted_data, f)
    else:
        return formatted_data


def to_dataset_pickle((task_subject, skill, correct, task_start, features, stim_pairs)):
    from io import Dataset
    headers = [('subject', Dataset.ENUM),
               ('skill', Dataset.ENUM),
               ('correct', Dataset.INT),
               ('task_start', Dataset.TIME),
               ('features', Dataset.MAT)]


if __name__ == "__main__":
    task_name, eeg_name = 'data/task_data4.gz', 'data/eeg_data4.gz'
    convert_task_from_xls('raw_data/task_large.xls', task_name)
    convert_eeg_from_xls('raw_data/eeg_data_thinkgear_2013_2014.xls', eeg_name)
    align_data('data/task_data4.gz', 'data/eeg_data4.gz', 'data/data4.gz')