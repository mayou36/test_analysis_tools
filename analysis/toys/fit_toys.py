#!/usr/bin/env python
# -*- coding: utf-8 -*-
# =============================================================================
# @file   fit_toys.py
# @author Albert Puig (albert.puig@cern.ch)
# @date   17.01.2017
# =============================================================================
"""Fit toys generated by `generate_toys.py`."""

import os
import argparse
from collections import OrderedDict, defaultdict
from timeit import default_timer

from scipy.stats import poisson
import pandas as pd

import ROOT

from analysis import get_global_var
from analysis.physics import get_physics_factory
from analysis.utils.logging_color import get_logger
from analysis.utils.monitoring import memory_usage
import analysis.utils.paths as _paths
import analysis.utils.config as _config
import analysis.utils.data as _data
import analysis.utils.root as _root


logger = get_logger('analysis.toys.fit')


def load_pdfs(model):
    """Load the PDFs of the given model.

    This includes configuring parameters.

    Arguments:
        model (list[dict]): Model to load.

    Returns:
        tuple[dict, list]: Physics factories by name, and external constraints
            to apply to the fit (if any).

    Raises:
        KeyError: If some configuration data are missing.
        ValueError: If there is any problem in configuring the PDF factories.

    """
    # Get PDFs
    model_observables = None
    physics_factories = {}
    fit_parameters = {}
    constraints = []
    for name, element in model.items():
        pdfs = element['pdfs']
        try:
            physics_factory = get_physics_factory(pdfs)
        except KeyError:
            logger.error("Cannot find physics factory for %s",
                         ','.join(['%s:%s' % (obs, pdf['pdf'])
                                   for obs, pdf in pdfs.items()]))
            raise ValueError()
        physics_factories[name] = physics_factory
        observables = set(obs.GetName()
                          for obs in physics_factory.get_observables())
        if model_observables is None:
            model_observables = observables
        else:
            if model_observables != observables:
                logger.error("Mismatch in observables between PDFs.")
                raise KeyError()
        # Configure the variables
        for pdf in pdfs.values():
            fit_parameters = {param.GetName(): param
                              for param in physics_factory.get_fit_parameters()}
            # Rename parameters to param_name^{model_name} for easier visualization
            physics_factory.set_parameter_names({param_name: "%s^{%s}" % (param_name, name)
                                                 for param_name in fit_parameters})
            for parameter_name, param_config in pdf.get('parameter-constraints', {}).items():
                # Get the parameter
                try:
                    parameter = fit_parameters[parameter_name]
                except KeyError:
                    logger.error("Unknown parameter -> %s", parameter_name)
                    raise
                try:
                    constraint = _config.configure_parameter(parameter, param_config)
                    if constraint:
                        constraints.append(constraint)
                except KeyError as error:
                    logger.error("Unknown parameter action -> %s", error)
                    raise
                except ValueError as error:
                    logger.error("Wrong parameter configuration -> %s", error)
                    raise
    return physics_factories, constraints


def get_datasets(data_info, data_frames, transformers):
    """Build the datasets from the input toys.

    Arguments:
        data_info (list[dict]): Configuration of input toys.
        data_frames (list[pandas.DataFrame]): Data frames with the toy information.
        transformers (dict): Dataset transformation functions, with the name of the
            output dataset as key.

    Returns:
        tuple (dict (str: ROOT.RooDataSet), dict (str: int)): Datasets made of the
            combination of the several input sources with the transformations applied,
            and number of generated events per data sample.

    Raises:
        KeyError: If there is information missing from the data configuration.

    """
    dataset = None
    sample_sizes = {}
    for data_name, data in data_info.items():
        # Do poisson if it is extended
        sample_sizes[data_name] = poisson.rvs(data['nevents'])
        # Extract suitable number of rows and transform them
        rows = data_frames[data_name].sample(sample_sizes[data_name])
        # Append to merged dataset
        if dataset is None:
            dataset = rows
        else:
            dataset = pd.concat([dataset, rows])
    # Convert dataset to RooDataset
    return {ds_name: _data.dataset_from_pandas(transform(dataset),
                                               "data_%s" % ds_name, "data_%s" % ds_name)
            for ds_name, transform in transformers.items()},\
        sample_sizes


def run(config_files, link_from, verbose):
    """Run the script.

    Arguments:
        config_files (list[str]): Path to the configuration files.
        link_from (str): Path to link the results from.
        verbose (bool): Give verbose output?

    Raises:
        OSError: If there either the configuration file does not exist some
            of the input toys cannot be found.
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
        raise OSError("Cannot load configuration files: %s",
                      config_files)
    except _config.ConfigError as error:
        if 'fit/nfits' in error.missing_keys:
            logger.error("Number of fits not specified")
        if 'name' in error.missing_keys:
            logger.error("No name was specified in the config file!")
        if 'data' in error.missing_keys:
            logger.error("No input data specified in the config file!")
        raise KeyError("ConfigError raised -> %s" % error.missing_keys)
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
    try:
        fit_strategies = {strategy_name: get_global_var('FIT_STRATEGIES')[strategy_name]
                          for strategy_name
                          in config['fit'].get('strategies', ['simple'])}
    except KeyError as error:
        logger.error("Missing fit_strategy configuration -> %s", str(error))
        raise KeyError("Missing fit_strategy configuration")
    if not fit_strategies:
        logger.error("No fit strategies were specified in the config file!")
        raise KeyError()
    # Some info
    logger.info("Doing %s sample/fit sequences", config['fit']['nfits'])
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
    for source_name, data_source in config['data'].items():
        try:
            source_toy = data_source['source']
        except KeyError:
            logger.error("Data source not specified")
            raise
        # pylint: disable=E1101
        path = _paths.get_toy_path(source_toy)
        if not os.path.exists(path):
            raise OSError("Cannot find input toy -> %s" % path)
        with pd.HDFStore(path) as store:
            data[source_name] = store['data']
        # Generator values
        for var_name in data[source_name]['toy_info'].columns:
            if var_name in ('seed', 'jobid', 'nevents'):
                continue
            gen_values['%s^{%s}' % (var_name, source_name)] = data[source_name]['toy_info'][var_name][0]
    fit_models = {}
    for model_name, model in models.items():
        try:
            logger.info("Loading PDFs")
            physics_factories, constraints = load_pdfs(model)
        except ValueError:
            raise KeyError()
        fit_parameters = []
        # Build the fit PDF
        pdfs = OrderedDict()
        for factory_name, factory in physics_factories.items():
            observables = factory.get_observables()
            fit_params = factory.get_fit_parameters()
            # Make extended PDF
            pdfs[factory_name] = factory.get_extended_pdf()("%s^{%s}" % (config['name'], factory_name),
                                                            "%s^{%s}" % (config['name'], factory_name),
                                                            "N^{%s}" % factory_name,
                                                            config[model_name][factory_name].get('initial-yield',
                                                                                                 1000),
                                                            *(observables + fit_params))
            # Add everything to the fit parameters
            fit_parameters.extend(fit_params)
            fit_parameters.append(factory.get('N'))
        # Make RooAddPdf
        logger.info("Building fit pdf")
        if len(pdfs) > 1:
            fit_model = ROOT.RooAddPdf(config['name'], config['name'], _root.list_to_rooargset(pdfs))
        else:
            fit_model = pdfs.values()[0]
        # Configure fit
        fit_config = [ROOT.RooFit.Save(True),
                      ROOT.RooFit.Extended(True),
                      ROOT.RooFit.Minos(config['fit'].get('minos', True)),
                      ROOT.RooFit.PrintLevel(2 if verbose else -1)]
        if constraints:
            constraints_set = _root.list_to_rooargset(constraints)
            fit_config.append(ROOT.RooFit.ExternalConstraints(constraints_set))
        fit_models[model_name] = (physics_factories, constraints, fit_parameters, pdfs, fit_model, fit_config)
    # TODO: Acceptance
    # acceptance = None
    # if 'acceptance' in config:
    #     acceptance_vars = config['acceptance']['vars']
    #     gen_file = config['acceptance']['gen-file']
    #     reco_file = config['acceptance']['reco-file']
    # Prepare output
    gen_events = defaultdict(list)
    # Set seed
    try:
        job_id = os.environ['PBS_JOBID']
        seed = int(job_id.split('.')[0])
    except KeyError:
        import random
        job_id = 'local'
        seed = random.randint(0, 100000)
    ROOT.RooRandom.randomGenerator().SetSeed(seed)
    # Start looping
    fit_results = defaultdict(list)
    logger.info("Starting sampling-fit loop (print frequency is 20)")
    initial_mem = memory_usage()
    initial_time = default_timer()
    for fit_num in range(config['fit']['nfits']):
        # Logging
        if (fit_num+1) % 20 == 0:
            logger.info("  Fitting event %s/%s", fit_num+1, config['fit']['nfits'])
        # Get a compound dataset
        try:
            logger.debug("Sampling input data")
            datasets, sample_sizes = get_datasets(config['data'],
                                                  data,
                                                  {model_name: getattr(model[0].values()[0],
                                                                       'transform_dataset')
                                                   for model_name, model in fit_models.items()})
            for sample_name, sample_size in sample_sizes.items():
                gen_events['N^{%s}_{gen}' % sample_name].append(sample_size)
        except KeyError:
            logger.exception("Bad data configuration")
            raise
        logger.debug("Fitting")
        for model_name in models:
            dataset = datasets.pop(model_name)
            physics_factories, constraints, fit_parameters, pdfs, fit_model, fit_config = fit_models[model_name]
            # Now fit
            for fit_name, fit_func in fit_strategies.items():
                toy_key = (model_name, fit_name)
                fit_result = fit_func(fit_model, dataset, fit_config)  # fit_model.fitTo(dataset, *fit_config)
                # Now results are in fit_parameters
                result = _data.fit_parameters_to_dict(fit_parameters)
                result['fit_status'] = fit_result.status()
                fit_results[toy_key].append(result)
                _root.destruct_object(fit_result)
            _root.destruct_object(dataset)
        logger.debug("Cleaning up")
    logger.info("Fitting loop over")
    logger.info("--> Memory leakage: %.2f MB/sample-fit", (memory_usage() - initial_mem)/config['fit']['nfits'])
    logger.info("--> Spent %.0f ms/sample-fit", (default_timer() - initial_time)*1000.0/(config['fit']['nfits']))
    logger.info("Saving to disk")
    data_res = []
    # Get gen values for this model
    data_gen = {key + '_{gen}': val for key, val in gen_values.items()}
    nominal_yields = {'N^{%s}_{nominal}' % data_name: data_info['nevents']
                      for data_name, data_info
                      in config['data'].items()}
    # indices = ['model_name', 'fit_strategy'] + data_gen.keys() + nominal_yields.keys()
    for (model_name, fit_strategy), fits in fit_results.items():
        for fit_res in fits:
            fit_res = fit_res.copy()
            fit_res['model_name'] = model_name
            fit_res['fit_strategy'] = fit_strategy
            data_res.append(fit_res)
            # fit_res.update(data_gen)
            # fit_res.update(nominal_yields)
            # Calculate pulls
    data_frame = pd.DataFrame(data_res)
    gen_frame = pd.concat([pd.concat([pd.concat([pd.DataFrame(data_gen, index=[0]),
                                                 pd.DataFrame(nominal_yields, index=[0])],
                                                axis=1)]*data_frame.shape[0]).reset_index(drop=True),
                           pd.DataFrame(gen_events)],
                          axis=1)
    # Currently, fit_result_frame is not indexed. Could be improved in the future.
    # Should I just separate gen_frame from data_frame to save space?
    fit_result_frame = pd.concat([gen_frame,
                                  data_frame,
                                  _data.calculate_pulls(data_frame, gen_frame)],
                                 axis=1)
    try:
        # pylint: disable=E1101
        with _paths.work_on_file(config['name'],
                                 config.get('link-from', None),
                                 _paths.get_toy_fit_path) as toy_fit_file:
            with _data.modify_hdf(toy_fit_file) as hdf_file:
                hdf_file.append('fit_results', fit_result_frame)
                # Add link to generated samples for bookeeping
                if 'gen_info' not in hdf_file:
                    hdf_file.append('gen_info',
                                    pd.DataFrame([{'name': data_name,
                                                   'source': _paths.get_toy_path(data_info['source']),
                                                   'nevents': data_info['nevents']}
                                                  for data_name, data_info
                                                  in config['data'].items()]).set_index(['name']))
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
        logger.error("Problem configuring physics factories")
    except RuntimeError as error:
        exit_status = 4
        logger.exception("Error in fitting events")
    # pylint: disable=W0703
    except Exception as error:
        exit_status = 128
        logger.exception('Uncaught exception -> %s', repr(error))
    finally:
        parser.exit(exit_status)


if __name__ == "__main__":
    main()

# EOF
