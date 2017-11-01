#!/usr/bin/env python
# -*- coding: utf-8 -*-
# =============================================================================
# @file   fit_toys.py
# @author Albert Puig (albert.puig@cern.ch)
# @date   17.01.2017
# =============================================================================
"""Fit toys generated by `generate_toys.py`."""

import argparse
import os
from collections import defaultdict
from timeit import default_timer

from scipy.stats import poisson
import numpy as np
import pandas as pd

import ROOT

from analysis.utils.logging_color import get_logger
from analysis.utils.monitoring import memory_usage
from analysis.data import get_data
from analysis.data.hdf import modify_hdf
from analysis.data.converters import dataset_from_pandas
from analysis.physics import configure_model
from analysis.efficiency import get_acceptance
from analysis.fit import fit
from analysis.fit.result import FitResult
from analysis.batch import get_job_id
import analysis.utils.paths as _paths
import analysis.utils.config as _config
import analysis.utils.root as _root
import analysis.utils.fit as _fit


logger = get_logger('analysis.toys.fit')


def get_datasets(data_frames, acceptance, fit_models):
    """Build the datasets from the input toys.

    If an acceptance is specified, events are selected using accept-reject.

    Arguments:
        data_frames (dict[tuple(pandas.DataFrame, int, str)]): Data frames with the requested
            number of events and the corresponding category.
        acceptance (analysis.efficiency.acceptance.Acceptance): Acceptance description.
            Can be None, in which case it is ignored.
        fit_models (dict): Fit models to use to transform datasets and (possibly) establish
            the data categories, with the name of the output as key.
        categories (dict, optional): Category of each data frame.

    Return:
        tuple (dict (str: ROOT.RooDataSet), dict (str: int)): Datasets made of the
            combination of the several input sources with the transformations applied,
            and number of generated events per data sample.

    Raise:
        KeyError: If there is information missing from the data configuration.

    """
    dataset = None
    sample_sizes = {}
    weight_var = None
    logger.debug("Sampling datasets -> %s", data_frames.keys())
    for data_name, (data, n_events, category) in data_frames.items():
        if acceptance:
            data = acceptance.apply_accept_reject(data)
        # Do poisson if it is extended
        sample_sizes[data_name] = poisson.rvs(n_events)
        # Extract suitable number of rows and transform them
        rows = data.sample(sample_sizes[data_name])
        # Add category column
        if category:
            # By default the label is stored in the 'category' column
            rows['category'] = category
        # Append to merged dataset
        if dataset is None:
            dataset = rows
        else:
            dataset = pd.concat([dataset, rows])
    logger.debug("Done loading")
    # Get the fit weight
    if acceptance:
        logger.debug("Adding fitting weights")
        weight_var = 'fit_weight'
        dataset[weight_var] = acceptance.get_fit_weights(dataset)
    # Convert dataset to RooDataset
    try:
        # TODO: Check the categories
        return ({ds_name: dataset_from_pandas(model.transform_dataset(dataset),
                                              "data_{}".format(ds_name),
                                              "data_{}".format(ds_name),
                                              weight_var=weight_var,
                                              categories=model.get_category_vars())
                 for ds_name, model in fit_models.items()},
                sample_sizes)
    except KeyError:
        logger.error("Error transforming dataset.")
        raise


def run(config_files, link_from, verbose):
    """Run the script.

    Arguments:
        config_files (list[str]): Path to the configuration files.
        link_from (str): Path to link the results from.
        verbose (bool): Give verbose output?

    Raise:
        OSError: If there either the configuration file does not exist some
            of the input toys cannot be found.
        AttributeError: If the input data are incompatible with a previous fit.
        KeyError: If some configuration data are missing.
        ValueError: If there is any problem in configuring the PDF factories.
        RuntimeError: If there is a problem during the fitting.

    """
    try:
        config = _config.load_config(*config_files,
                                     validate=['fit/nfits',
                                               'name',
                                               'data'])
    except OSError:
        raise OSError("Cannot load configuration files: {}".format(config_files))
    except _config.ConfigError as error:
        if 'fit/nfits' in error.missing_keys:
            logger.error("Number of fits not specified")
        if 'name' in error.missing_keys:
            logger.error("No name was specified in the config file!")
        if 'data' in error.missing_keys:
            logger.error("No input data specified in the config file!")
        raise KeyError("ConfigError raised -> {}".format(error.missing_keys))
    except KeyError as error:
        logger.error("YAML parsing error -> %s", error)
    try:
        models = {model_name: config[model_name]
                  for model_name
                  in config['fit'].get('models', ['model'])}
    except KeyError as error:
        logger.error("Missing model configuration -> %s", str(error))
        raise KeyError("Missing model configuration")
    if not models:
        logger.error("No model was specified in the config file!")
        raise KeyError()
    fit_strategies = config['fit'].get('strategies', ['simple'])
    if not fit_strategies:
        logger.error("Empty fit strategies were specified in the config file!")
        raise KeyError()
    # Some info
    nfits = config['fit'].get('nfits-per-job', config['fit']['nfits'])
    logger.info("Doing %s sample/fit sequences", nfits)
    logger.info("Fit job name: %s", config['name'])
    if link_from:
        config['link-from'] = link_from
    if 'link-from' in config:
        logger.info("Linking toy data from %s", config['link-from'])
    else:
        logger.debug("No linking specified")
    # Analyze data requirements
    logger.info("Loading input data")
    data = {}
    gen_values = {}
    if len(set('category' in data_source for data_source in config['data'])) > 1:
        raise KeyError("Categories in 'data' not consistently specified.")
    for data_id, data_source in config['data'].items():
        try:
            source_toy = data_source['source']
        except KeyError:
            logger.error("Data source not specified")
            raise
        data[data_id] = (get_data({'source': source_toy,
                                   'source-type': 'toy',
                                   'tree': 'data',
                                   'output-format': 'pandas',
                                   'selection': data_source.get('selection', None)}),
                         data_source['nevents'],
                         data_source.get('category', None))
        # Generator values
        toy_info = get_data({'source': source_toy,
                             'source-type': 'toy',
                             'tree': 'toy_info',
                             'output-format': 'pandas'})
        gen_values[data_id] = {}
        for var_name in toy_info.columns:
            if var_name in ('seed', 'jobid', 'nevents'):
                continue
            gen_values[data_id][var_name] = toy_info[var_name].iloc[0]
    try:
        fit_models = {}
        for model_name in models:
            if model_name not in config:
                raise KeyError("Missing model definition -> {}".format(model_name))
            fit_models[model_name] = configure_model(config[model_name])
    except KeyError:
        logger.exception('Error loading model')
        raise ValueError('Error loading model')
    # Let's check these generator values against the output file
    try:
        gen_values_frame = {}
        # pylint: disable=E1101
        with _paths.work_on_file(config['name'],
                                 _paths.get_toy_fit_path,
                                 config.get('link-from', None)) as toy_fit_file:
            with pd.HDFStore(toy_fit_file, mode='w') as hdf_file:
                logger.debug("Checking generator values")
                test_gen = [('gen_{}'.format(data_source)) in hdf_file
                            for data_source in gen_values]
                if all(test_gen):  # The data were written already, crosscheck values
                    for source_id, gen_value in gen_values.items():
                        if not all(hdf_file['gen_{}'.format(data_source)][var_name].iloc[0] == var_value
                                   for var_name, var_value in gen_value.items()):
                            raise AttributeError(
                                "Generated and stored values don't match for source '{}'".format(source_id))
                elif not any(test_gen):  # No data were there, just overwrite
                    for source_id, gen_values in gen_values.items():
                        gen_data = {'id': source_id,
                                    'source': _paths.get_toy_path(config['data'][source_id]['source']),
                                    'nevents': config['data'][source_id]['nevents']}
                        gen_data.update(gen_values)
                        gen_values_frame['gen_{}'.format(source_id)] = pd.DataFrame([gen_data])
                else:
                    raise AttributeError("Inconsistent number of data sources")
    except OSError, excp:
        logger.error(str(excp))
        raise
    # Now load the acceptance
    try:
        acceptance = get_acceptance(config['acceptance']) \
            if 'acceptance' in config \
            else None
    except _config.ConfigError as error:
        raise KeyError("Error loading acceptance -> {}".format(error))
    # Prepare output
    gen_events = defaultdict(list)
    # Set seed
    job_id = get_job_id()
    if job_id:
        seed = int(job_id.split('.')[0])
    else:
        import random
        job_id = 'local'
        seed = random.randint(0, 100000)
    np.random.seed(seed=seed)
    ROOT.RooRandom.randomGenerator().SetSeed(seed)
    # Start looping
    fit_results = defaultdict(list)
    logger.info("Starting sampling-fit loop (print frequency is 20)")
    initial_mem = memory_usage()
    initial_time = default_timer()
    for fit_num in range(nfits):
        # Logging
        if (fit_num+1) % 20 == 0:
            logger.info("  Fitting event %s/%s", fit_num+1, nfits)
        # Get a compound dataset
        try:
            logger.debug("Sampling input data")
            datasets, sample_sizes = get_datasets(data,
                                                  acceptance,
                                                  fit_models)
            for sample_name, sample_size in sample_sizes.items():
                gen_events['N^{{{}}}_{{gen}}'.format(sample_name)].append(sample_size)
            logger.debug("Sampling finalized")
        except KeyError:
            logger.exception("Bad data configuration")
            raise
        logger.debug("Fitting")
        for model_name in models:
            dataset = datasets.pop(model_name)
            fit_model = fit_models[model_name]
            # Now fit
            for fit_strategy in fit_strategies:
                toy_key = (model_name, fit_strategy)
                try:
                    fit_result = fit(fit_model,
                                     model_name,
                                     fit_strategy,
                                     dataset,
                                     verbose,
                                     Extended=config['fit'].get('extended', True),
                                     Minos=config['fit'].get('minos', True))
                except ValueError:
                    raise RuntimeError()
                # Now results are in fit_parameters
                result_roofit = FitResult().from_roofit(fit_result)
                result = result_roofit.to_plain_dict()
                result['cov_matrix'] = result_roofit.get_covariance_matrix()
                result['fitnum'] = fit_num
                fit_results[toy_key].append(result)
                _root.destruct_object(fit_result)
            _root.destruct_object(dataset)
        logger.debug("Cleaning up")
    logger.info("Fitting loop over")
    logger.info("--> Memory leakage: %.2f MB/sample-fit", (memory_usage() - initial_mem)/nfits)
    logger.info("--> Spent %.0f ms/sample-fit", (default_timer() - initial_time)*1000.0/nfits)
    logger.info("Saving to disk")
    data_res = []
    cov_matrices = {}
    # Get gen values for this model
    for (model_name, fit_strategy), fits in fit_results.items():
        for fit_res in fits:
            fit_res = fit_res.copy()
            fit_res['model_name'] = model_name
            fit_res['fit_strategy'] = fit_strategy

            cov_folder = os.path.join(str(job_id), str(fit_res['fitnum']))
            cov_matrices[cov_folder] = pd.DataFrame(fit_res.pop('cov_matrix'))
            data_res.append(fit_res)
    data_frame = pd.DataFrame(data_res)
    fit_result_frame = pd.concat([pd.DataFrame(gen_events),
                                  data_frame,
                                  pd.concat([pd.DataFrame({'seed': [seed],
                                                           'jobid': [job_id]})]
                                            * data_frame.shape[0]).reset_index(drop=True)],
                                 axis=1)
    try:
        # pylint: disable=E1101
        with _paths.work_on_file(config['name'],
                                 path_func=_paths.get_toy_fit_path,
                                 link_from=config.get('link-from', None)) as toy_fit_file:
            with modify_hdf(toy_fit_file) as hdf_file:
                # First fit results
                hdf_file.append('fit_results', fit_result_frame)
                # Save covarinance matrix under 'covariance/jobid/fitnum
                for cov_folder, cov_matrix in cov_matrices.items():
                    cov_path = os.path.join('covariance', cov_folder)
                    hdf_file.append(cov_path, cov_matrix)
                # Generator info
                for key_name, gen_frame in gen_values_frame.items():
                    hdf_file.append(key_name, gen_frame)

                # save cov matrix with for loop over job_id/fitnum
            logger.info("Written output to %s", toy_fit_file)
            if 'link-from' in config:
                logger.info("Linked to %s", config['link-from'])
    except OSError, excp:
        logger.error(str(excp))
        raise
    except ValueError as error:
        logger.exception("Exception on dataset saving")
        raise RuntimeError(str(error))


def main():
    """Toy fitting application.

    Parses the command line and fits the toys, catching intermediate
    errors and transforming them to status codes.

    Status codes:
        0: All good.
        1: Error in the configuration files.
        2: Files missing (configuration or toys).
        3: Error configuring physics factories.
        4: Error in the event generation. An exception is logged.
        5: Input data is inconsistent with previous fits.
        128: Uncaught error. An exception is logged.

    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose',
                        action='store_true',
                        help="Verbose output")
    parser.add_argument('--link-from',
                        action='store', type=str, default='',
                        help="Folder to actually store the fit results")
    parser.add_argument('config',
                        action='store', type=str, nargs='+',
                        help="Configuration files")
    args = parser.parse_args()
    if args.verbose:
        get_logger('analysis').setLevel(1)
        logger.setLevel(1)
    else:
        ROOT.RooMsgService.instance().setGlobalKillBelow(ROOT.RooFit.WARNING)
    try:
        exit_status = 0
        run(args.config, args.link_from, args.verbose)
    except KeyError:
        exit_status = 1
        logger.exception("Bad configuration given")
    except OSError, error:
        exit_status = 2
        logger.error(str(error))
    except ValueError:
        exit_status = 3
        logger.exception("Problem configuring physics factories")
    except RuntimeError as error:
        exit_status = 4
        logger.error("Error in fitting events")
    except AttributeError as error:
        exit_status = 5
        logger.error("Inconsistent input data -> %s" % error)
    # pylint: disable=W0703
    except Exception as error:
        exit_status = 128
        logger.exception('Uncaught exception -> %s', repr(error))
    finally:
        parser.exit(exit_status)


if __name__ == "__main__":
    main()

# EOF
