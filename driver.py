import sys
import time
import inspect
import cPickle
import gzip
import argparse
from operator import or_
from collections import namedtuple
import random
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import numpy
import theano
import theano.tensor as T

from model.mlp import MLP, HiddenNetwork, rectifier
from model.vector import VectorLayer
from libs.utils import (make_shared, random_unique_subset,
                        idx_to_mask, transpose)
import config
from libs.data import gen_word_matrix
from libs.logger import gen_log_name, set_log_file, log, log_args
from itertools import imap, islice, groupby, chain, compress
from libs.auc import auc

BNTSM_TIME_FORMAT = '%m/%d/%y %I:%M %p'


def get_val(tensor):
    return tensor.get_value(borrow=True)


def normalize_table(table):
    table = numpy.array(table)
    mins = table.min(axis=0)
    maxs = table.max(axis=0)
    return (table - mins) / (maxs - mins)


def prepare_eeglrkt_data(dataset_name='raw_data/eeglrkt/evidence.2013_2014.txt', held_out_subj=3):
    log('... loading data', True)
    log_args(inspect.currentframe())

    from libs.loader import load
    eeg_headers = ('kc_med', 'kc_att', 'kc_raww', 'kc_delta', 'kc_theta',
                   'kc_alpha', 'kc_beta')
    data, enum_dict, _ = load(
        dataset_name,
        numeric=['fluent'],
        time=['start_time'],
        numeric_float=eeg_headers,
        enum=['user', 'skill'],
        time_format=BNTSM_TIME_FORMAT)
    sorted_idxs, _ = transpose(sorted(enumerate(data['start_time']),
                                      key=lambda v: v[1]))
    subject_x = data['user'][sorted_idxs]
    skill_x = data['skill'][sorted_idxs]
    start_x = data['start_time'][sorted_idxs]
    correct_y = data['fluent'][sorted_idxs]
    eeg_x = numpy.column_stack([data[eh] for eh in eeg_headers])[sorted_idxs, :]
    stim_pairs = list(enum_dict['skill'].iteritems())
    sorted_subj = sorted(numpy.unique(subject_x),
                         key=lambda s: sum(numpy.equal(subject_x, s)))
    held_out_subj = sorted_subj[random.randint(1, 10)]
    valid_subj_mask = numpy.equal(subject_x, held_out_subj)
    log('subjects {} are held out'.format(held_out_subj), True)
    train_idx = numpy.nonzero(numpy.logical_not(valid_subj_mask))[0]
    valid_idx = numpy.nonzero(valid_subj_mask)[0]

    return (subject_x, skill_x, correct_y, start_x, eeg_x, stim_pairs, train_idx, valid_idx)


def save_prepared_data_to_eeglrkt(fname, prepared_data):
    from libs.loader import save
    from libs.utils import combine_dict

    (subject_x, skill_x, correct_y, start_x, eeg_x, stim_pairs, train_idx, valid_idx) = prepared_data
    # Aaron: I need to convert eeg_x to a proper numpy int array in general but I haven't done it yet
    eeg_x = numpy.array([list(e) for e in eeg_x])
    eeg_columns = {}
    eeg_bands = ('kc_delta', 'kc_theta', 'kc_alpha', 'kc_beta')
    for i, band in enumerate(eeg_bands):
        eeg_columns[band] = eeg_x[:, i]
    zero_eeg_bands = ('kc_att', 'kc_med', 'kc_raww', 'kc_gamma')
    for i, band in enumerate(zero_eeg_bands):
        eeg_columns[band] = numpy.zeros(eeg_x.shape[0])
    save(fname,
         numeric=combine_dict(eeg_columns, {'user': subject_x, 'fluent': correct_y}),
         enum={'skill': skill_x},
         enum_dict={'skill': dict(stim_pairs)},
         time={'start_time': start_x},
         time_format=BNTSM_TIME_FORMAT,
         header=['user', 'skill', 'fluent', 'start_time'] + list(eeg_bands) + list(zero_eeg_bands))
    sys.exit()


def prepare_data(dataset_name, top_n=0, top_eeg_n=0, eeg_only=0, normalize=0, **kwargs):
    log('... loading data', True)
    log_args(inspect.currentframe())

    with gzip.open(dataset_name, 'rb') as f:
        subject_x, skill_x, correct_y, start_x, eeg_x, stim_pairs = cPickle.load(f)
    subjects = numpy.unique(subject_x)
    indexable_eeg = numpy.asarray(eeg_x)

    def row_count(subj):
        return sum(numpy.equal(subject_x, subj))

    def eeg_count(subj):
        arr = indexable_eeg[numpy.equal(subject_x, subj)]
        return sum(numpy.not_equal(arr, None))

    # select only the subjects that have enough data
    if top_n:
        subjects = sorted(subjects, key=row_count)[-top_n:]
    if top_eeg_n:
        subjects = sorted(subjects, key=eeg_count)[-top_eeg_n:]
    mask = reduce(or_, imap(lambda s: numpy.equal(subject_x, s), subjects))

    # normalize eegs
    eeg_mask = numpy.not_equal(indexable_eeg, None)
    if normalize:
        for s in subjects:
            subj_mask = numpy.equal(subject_x, s)
            subj_eeg_mask = subj_mask & eeg_mask
            table = numpy.array([list(l) for l in indexable_eeg[subj_eeg_mask]])
            table = normalize_table(table)
            idxs = numpy.nonzero(subj_eeg_mask)
            for i in range(len(idxs)):
                eeg_x[i] = table[i]

    # mask out unselected data
    if eeg_only:
        mask &= eeg_mask
    subject_x = subject_x[mask]
    skill_x = skill_x[mask]
    correct_y = correct_y[mask]
    start_x = start_x[mask]
    eeg_x = list(compress(indexable_eeg, mask))

    # break cv folds
    valid_subj_mask = random_unique_subset(subject_x, .9)
    log('subjects {} are held out'.format(numpy.unique(subject_x[valid_subj_mask])), True)
    train_idx = numpy.nonzero(numpy.logical_not(valid_subj_mask))[0]
    valid_idx = numpy.nonzero(valid_subj_mask)[0]

    # import random
    # train_idx = random.sample(train_idx, 6000)
    # valid_idx = random.sample(valid_idx, min(len(valid_idx), 400))

    return (subject_x, skill_x, correct_y, start_x, eeg_x, stim_pairs, train_idx, valid_idx)


# look up tables are cheaper memory-wise.
# TODO: unify this implementation with VectorLayer
def to_lookup_table(x, access_idxs):
    mask = numpy.asarray([v is not None for v in x], dtype=bool)
    if not mask.any():
        raise Exception("can't create lookup table from no data")

    # create lookup table
    valid_idxs = numpy.nonzero(mask)[0]
    width = len(x[valid_idxs[0]])
    table = numpy.zeros((1 + len(valid_idxs), width))  # leave the first row for "None"
    for i, l in enumerate(compress(x, mask)):
        table[i + 1] = numpy.asarray(l)
    table[1:] = normalize_table(table[1:])
    table[0] = table[1:].mean(axis=0)  # set the "None" vector to the average of all vectors

    # create a way to index into lookup table
    idxs = numpy.zeros(len(x))
    idxs[valid_idxs] = xrange(1, len(valid_idxs) + 1)

    # convert to theano
    t_table = make_shared(table)
    t_idxs = make_shared(idxs, to_int=True)
    return t_table[t_idxs[access_idxs]], table.shape


def build_model(prepared_data, L1_reg, L2_reg, dropout_p, learning_rate,
                skill_vector_len=100, combiner_depth=1, combiner_width=200,
                main_net_depth=1, main_net_width=500, previous_eeg_on=1,
                current_eeg_on=1, mutable_skill=1, valid_percentage=0.8, **kwargs):
    log('... building the model', True)
    log_args(inspect.currentframe())

    # ##########
    # STEP1: order the data properly so that we can read from it sequentially
    # when training the model

    subject_x, skill_x, correct_y, start_x, eeg_x, stim_pairs, train_idx, valid_idx = prepared_data
    correct_y += 1
    subject_x = subject_x[:, None]  # add extra dimension as a 'feature vector'
    skill_x = skill_x[:, None]  # add extra dimension as a 'feature vector'
    train_mask = idx_to_mask(train_idx, len(subject_x))
    valid_mask = idx_to_mask(valid_idx, len(subject_x))

    # reorder indices so that each index can be fed as a 'base_index' into the
    # full model. This means lining up by subjects and removing the first few indices.
    # first we sort by subject
    sorted_i = [i for (i, v) in
                sorted(enumerate(subject_x), key=lambda (i, v): v)]
    skill_x = skill_x[sorted_i]
    subject_x = subject_x[sorted_i]
    correct_y = correct_y[sorted_i]
    train_mask = train_mask[sorted_i]
    valid_mask = valid_mask[sorted_i]
    # then we get rid of the first few indices per subject
    subject_groups = groupby(range(len(subject_x)), lambda (i): subject_x[i, 0])
    good_indices = list(chain.from_iterable(
        imap(lambda (_, g): islice(g, 2, None), subject_groups)))

    t_good_indices = make_shared(good_indices, to_int=True)
    master_indices = T.ivector('idx')
    base_indices = t_good_indices[master_indices]

    # select only the good indices from CV indices
    good_mask = idx_to_mask(good_indices, len(subject_x))
    train_idx = numpy.nonzero(good_mask & train_mask)[0]
    valid_idx = numpy.nonzero(good_mask & valid_mask)[0]

    log('training set size: {}'.format(len(train_idx)))
    log('validation set size: {}'.format(len(valid_idx)))

    skill_x = make_shared(skill_x)
    subject_x = make_shared(subject_x)
    correct_y = make_shared(correct_y, to_int=True)

    # ###########
    # STEP2: connect up the model. See figures/vector_edu_model.png for diagram
    # TODO: make the above mentioned diagram

    NetInput = namedtuple("NetInput", ['input', 'size'])
    rng = numpy.random.RandomState(1234)
    t_dropout = T.scalar('dropout')
    y = correct_y[base_indices]

    # setup base-input layers
    skill_matrix = numpy.asarray(gen_word_matrix(skill_x.get_value(borrow=True),
                                                 stim_pairs,
                                                 vector_length=skill_vector_len),
                                 dtype=theano.config.floatX)
    current_skill = VectorLayer(rng=rng,
                                indices=base_indices,
                                full_input=skill_x,
                                vectors=skill_matrix,
                                mutable=mutable_skill)
    previous_skill = VectorLayer(rng=rng,
                                 indices=base_indices - 1,
                                 full_input=skill_x,
                                 vectors=skill_matrix,
                                 mutable=mutable_skill)

    # setup combiner component
    skill_accumulator = make_shared(numpy.zeros(
        (skill_x.get_value(borrow=True).shape[0], combiner_width)))
    correct_feature = make_shared([[0], [1]])[correct_y[base_indices - 1] - 1]
    combiner_inputs = [NetInput(skill_accumulator[base_indices - 2], combiner_width),
                       NetInput(previous_skill.output, skill_vector_len),
                       NetInput(correct_feature, 1)]
    if previous_eeg_on:
        eeg_vector, (_, eeg_vector_len) = to_lookup_table(eeg_x, base_indices - 1)
        combiner_inputs.append(NetInput(eeg_vector, eeg_vector_len))
    combiner = HiddenNetwork(
        rng=rng,
        input=T.concatenate([c.input for c in combiner_inputs], axis=1),
        n_in=sum(c.size for c in combiner_inputs),
        size=[combiner_width] * combiner_depth,
        activation=rectifier,
        dropout=t_dropout
    )

    # setup main network component
    classifier_inputs = [NetInput(combiner.output, combiner_width),
                         NetInput(current_skill.output, skill_vector_len)]
    if current_eeg_on:
        eeg_vector, (_, eeg_vector_len) = to_lookup_table(eeg_x, base_indices)
        classifier_inputs.append(NetInput(eeg_vector, eeg_vector_len))
    classifier = MLP(rng=rng,
                     n_in=sum(c.size for c in classifier_inputs),
                     input=T.concatenate([c.input for c in classifier_inputs], axis=1),
                     size=[main_net_width] * main_net_depth,
                     n_out=3,
                     dropout=t_dropout)

    # ########
    # STEP3: create the theano functions to run the model

    subnets = (combiner, classifier)
    cost = (
        classifier.negative_log_likelihood(y)
        + L1_reg * sum([n.L1 for n in subnets])
        + L2_reg * sum([n.L2_sqr for n in subnets])
    )

    func_args = {
        'inputs': [base_indices],
        'outputs': [classifier.errors(y), classifier.output],
        'on_unused_input': 'ignore',
        'allow_input_downcast': True
    }

    params = chain(current_skill.params, *[n.params for n in subnets])
    update_parameters = [(param, param - learning_rate * T.grad(cost, param))
                         for param in params]
    update_accumulator = [(
        skill_accumulator,
        T.set_subtensor(skill_accumulator[base_indices - 1], combiner.output)
    )]
    f_valid = theano.function(
        updates=update_accumulator,
        givens={t_dropout: 0.},
        **func_args)
    f_train = theano.function(
        updates=update_parameters + update_accumulator,
        givens={t_dropout: dropout_p},
        **func_args)

    def train_eval(pred):
        _y = correct_y.owner.inputs[0].get_value(borrow=True)[train_idx]
        return auc(_y[:len(pred)], pred, pos_label=2)

    def valid_eval(pred):
        _y = correct_y.owner.inputs[0].get_value(borrow=True)[valid_idx]
        return auc(_y[:len(pred)], pred, pos_label=2)
    return f_train, f_valid, train_idx, valid_idx, train_eval, valid_eval


def train_model(train_model, validate_model, train_idx, valid_idx,
                train_eval, valid_eval, batch_size, n_epochs):
    log('... training', True)

    patience = 50  # look as this many examples regardless
    patience_increase = 40
    improvement_threshold = 1
    validation_frequency = 5
    best_valid_error = numpy.inf
    best_epoch = 0

    for epoch in range(n_epochs):
        # before the skill_accumulator is setup properly, train one at a time
        # _batch_size = 1 if epoch == 0 else batch_size
        _batch_size = batch_size
        train_results = [train_model(train_idx[i * _batch_size: (i + 1) * _batch_size])
                         for i in xrange(int(len(train_idx) / _batch_size))]
        # Aaron: this is not really a speed critical part of the code but we
        # can come back and redo AUC in theano if we want to make this suck less
        train_preds = list(chain.from_iterable(imap(lambda r: r[1], train_results)))
        train_error = train_eval(train_preds)
        log('epoch {epoch}, train error {err:.2%}'.format(
            epoch=epoch, err=train_error), True)

        if (epoch + 1) % validation_frequency == 0:
            _batch_size = 1  # recreate the skill_accumulator each time
            results = [validate_model(valid_idx[i * _batch_size: (i + 1) * _batch_size])
                       for i in xrange(int(len(valid_idx) / _batch_size))]
            # Aaron: this is not really a speed critical part of the code but we
            # can come back and redo AUC in theano if we want to make this suck less
            preds = list(chain.from_iterable(imap(lambda r: r[1], results)))
            valid_error = valid_eval(preds)
            log('epoch {epoch}, validation error {err:.2%}'.format(
                epoch=epoch, err=valid_error), True)

            if valid_error < best_valid_error:
                if (valid_error < best_valid_error * improvement_threshold):
                    patience = max(patience, epoch + patience_increase)
                best_valid_error = valid_error
                best_epoch = epoch

            if patience <= epoch:
                break
    return best_valid_error, best_epoch


def run(learning_rate=0.01, L1_reg=0.00, L2_reg=0.0001, n_epochs=500,
        dataset_name='data/data.gz', batch_size=30, dropout_p=0.2, **kwargs):
    log_args(inspect.currentframe())
    import kt.data
    prepared_data = kt.data.prepare_data(dataset_name, **kwargs)
    # prepared_data = prepare_eeglrkt_data(dataset_name)
    # save_prepared_data_to_eeglrkt('evidence_rebuilt.2012_2013.xls', prepared_data)

    f_train, f_validate, train_idx, valid_idx, train_eval, valid_eval = (
        build_model(prepared_data, L1_reg=L1_reg, L2_reg=L2_reg,
                    dropout_p=dropout_p, learning_rate=learning_rate, **kwargs))

    start_time = time.clock()
    best_validation_loss, best_epoch = (
        train_model(f_train, f_validate, train_idx, valid_idx, train_eval, valid_eval,
                    batch_size, n_epochs=n_epochs))
    end_time = time.clock()
    training_time = (end_time - start_time) / 60.

    log(('Optimization complete. Best validation score of %f %%') %
        (best_validation_loss * 100.), True)
    log('Code ran for ran for %.2fm' % (training_time))
    return (best_validation_loss * 100., best_epoch + 1, training_time)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="run a theano experiment on this computer")
    parser.add_argument('-p', dest='param_set', type=str, default='default',
                        choices=config.all_param_set_keys,
                        help='the name of the parameter set that we want to use')
    parser.add_argument('--f', dest='file', type=str, default='data/data4.gz',
                        help='the data file to use')
    parser.add_argument('-o', dest='outname', type=str, default=gen_log_name(),
                        help='name for the log file to be generated')
    args = parser.parse_args()

    params = config.get_config(args.param_set)
    set_log_file(args.outname)
    log(run(dataset_name=args.file, **params))
    print "finished"
    if sys.platform.startswith('win'):
        from win_utils import winalert
        winalert()
