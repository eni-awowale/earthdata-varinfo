""" This module contains a class designed to read information from a `.dmr`
    file. This should group the input into science variables, metadata,
    coordinates, dimensions and ancillary data sets.

"""
from abc import ABC, abstractmethod
from logging import Logger
from typing import Dict, Optional, Set, Union
import re
import xml.etree.ElementTree as ET
import yaml

from netCDF4 import Dataset, Group

from varinfo.cf_config import CFConfig
from varinfo.utilities import (DAP4_TO_NUMPY_MAP, get_xml_namespace,
                               split_attribute_path, recursive_get)
from varinfo.variable import VariableFromDmr, VariableFromNetCDF4


OutputVariableType = Union[VariableFromDmr]


class VarInfoBase(ABC):
    """ An abstract base class to represent the full dataset of a granule,
        having reading information from a representation of that granule.

        A class to represent the full dataset of a granule, by parsing a `.dmr`
        file from OPeNDAP.

    """

    def __init__(self, file_path: str, logger: Logger,
                 config_file: Optional[str] = None):
        """ Distinguish between variables containing references to other
            datasets, and those that do not. The former are considered science
            variables, providing they are not considered coordinates or
            dimensions for another variable.

            Each variable contains references to their specific coordinates and
            dimensions, allowing the retrieval of all required variables for a
            specified list of science variables.

        """
        self.config_file = config_file
        self.logger = logger
        self.cf_config = None
        self.global_attributes = {}
        self.short_name = None
        self.mission = None
        self.namespace = None
        self.metadata_variables: Dict[str, OutputVariableType] = {}
        self.variables_with_coordinates: Dict[str, OutputVariableType] = {}
        self.references: Set[str] = set()
        self.metadata = {}

        self._set_var_info_config()
        self._read_dataset(file_path)
        self._set_global_attributes()
        self._set_mission_and_short_name()
        self._set_cf_config()
        self._update_global_attributes()
        self._extract_variables()

    @abstractmethod
    def _read_dataset(self, file_path: str):
        """ This method parses a file at the specified location using
            functionality specific to the type of input (e.g. a `.dmr` file).

        """

    @abstractmethod
    def _set_global_attributes(self):
        """ Extract the global attributes from the granule representation using
            functionality specific to the type of input.

        """

    @abstractmethod
    def _extract_variables(self):
        """ Iterate through all variables in the retrieved dataset. For each
            variable create an instance of a `Variable` (using the relavent
            child class), and assign it to either the `metadata_variables`
            or the `variable_with_coordinates` dictionary accordingly.

        """

    def _assign_variable(self, variable_object):
        """ Given a `Variable` instance, based on the content of the
            `coordinates` attribute, assign it to either the
            `variables_with_coordinates` or `metadata_variables` dictionary.
            Additionally, the set of references for all variables is updated.

        """
        full_path = variable_object.full_name_path
        self.references.update(variable_object.get_references())

        if variable_object.references.get('coordinates') is not None:
            self.variables_with_coordinates[full_path] = variable_object
        else:
            self.metadata_variables[full_path] = variable_object

    def _set_var_info_config(self):
        """ Read the VarInfo configuration YAML file, containing locations to
            search for the collection short_name attribute, and the mapping
            from short_name to satellite mission.

        """
        if self.config_file is not None:
            with open(self.config_file, 'r') as file_handler:
                self.var_info_config = yaml.load(file_handler, yaml.FullLoader)
        else:
            self.var_info_config = {}

    def _set_cf_config(self):
        """ Instantiate a CFConfig object, to contain any rules for exclusions,
            required fields and augmentations to CF attributes that are not
            contained within a granule from the specified collection.

        """
        self.cf_config = CFConfig(self.mission, self.short_name,
                                  self.config_file)

    def _set_mission_and_short_name(self):
        """ Check a series of potential locations for the collection short name
        of the granule. Once that is determined, match that short name to its
        associated mission.

        """
        self.short_name = next(
            (recursive_get(self.global_attributes, split_attribute_path(item))
             for item
             in self.var_info_config.get('Collection_ShortName_Path', [])
             if recursive_get(self.global_attributes,
                              split_attribute_path(item))
             is not None),
            None
        )

        if self.short_name is not None:
            self.mission = next((name
                                 for pattern, name
                                 in self.var_info_config.get('Mission', {}).items()
                                 if re.match(pattern, self.short_name)
                                 is not None), None)

    def _update_global_attributes(self):
        """ Having identified the mission and short_name for the granule, and
            therefore obtained the relevant CF configuration overrides and
            supplements, update the global attributes for this granule using
            the CFConfig class instance. As the overrides are assumed to have
            the strongest priority, the dictionary is updated with these values
            last.

        """
        if self.cf_config.global_supplements:
            self.global_attributes.update(self.cf_config.global_supplements)

        if self.cf_config.global_overrides:
            self.global_attributes.update(self.cf_config.global_overrides)

    def get_variable(self, variable_path: str) -> Optional[OutputVariableType]:
        """ Retrieve a variable specified by an absolute path. First check the
            variables with coordinates, before checking those without. If there
            are no matching variables, a value of `None` is returned.

        """
        return (self.variables_with_coordinates.get(variable_path) or
                self.metadata_variables.get(variable_path))

    def get_science_variables(self) -> Set[str]:
        """ Retrieve set of names for all variables that have coordinate
            references, that are not themselves used as dimensions, coordinates
            or ancillary date for another variable.

        """
        exclusions_pattern = re.compile(
            '|'.join(self.cf_config.excluded_science_variables)
        )

        filtered_with_coordinates = {
            variable
            for variable
            in self.variables_with_coordinates
            if variable is not None
            and not self.variable_is_excluded(variable, exclusions_pattern)
        }

        return filtered_with_coordinates - self.references

    def get_metadata_variables(self) -> Set[str]:
        """ Retrieve set of names for all variables that do no have
            coordaintes references, that are not themselves used as dimensions,
            coordinates or ancillary data for another variable.

            Additionally, any excluded science variables, that are contained
            in the variables_with_coordinates class attribute should be
            considered a metadata variable.

        """
        exclusions_pattern = re.compile(
            '|'.join(self.cf_config.excluded_science_variables)
        )

        additional_metadata = {
            variable
            for variable
            in self.variables_with_coordinates
            if variable is not None
            and self.variable_is_excluded(variable, exclusions_pattern)
        }

        metadata_variables = set(self.metadata_variables.keys())
        metadata_variables.update(additional_metadata)

        return metadata_variables - self.references

    @staticmethod
    def variable_is_excluded(variable_name: str,
                             exclusions_pattern: re.Pattern) -> bool:
        """ Ensure the variable name does not match any collection specific
            exclusion rules.

        """
        if exclusions_pattern.pattern != '':
            exclude_variable = exclusions_pattern.match(variable_name) is not None
        else:
            exclude_variable = False

        return exclude_variable

    def get_required_variables(self, requested_variables: Set[str]) -> Set[str]:
        """ Retrieve requested variables and recursively search for all
            associated dimension and coordinate variables. The returned set
            should be the union of the science variables, coordinates and
            dimensions.

            The requested variables are also augmented to include required
            variables for the collection, as indicated by the CFConfig class
            instance, and any references within those variables.

        """
        if self.cf_config.required_variables:
            cf_required_pattern = re.compile(
                '|'.join(self.cf_config.required_variables)
            )

            all_variable_names = set(self.variables_with_coordinates.keys()).union(
                set(self.metadata_variables.keys())
            )

            cf_required_variables = {variable
                                     for variable
                                     in all_variable_names
                                     if variable is not None
                                     and re.match(cf_required_pattern, variable)}
        else:
            cf_required_variables = set()

        requested_variables.update(cf_required_variables)
        required_variables: Set[str] = set()

        while len(requested_variables) > 0:
            variable_name = requested_variables.pop()
            required_variables.add(variable_name)

            variable = self.get_variable(variable_name)

            if variable is not None:
                # Add variable. Enqueue references not already present in
                # required set. (Also checking that they are real variables,
                # and not non-variable dimensions)
                variable_references = {
                    reference_variable
                    for reference_variable
                    in variable.get_references()
                    if self.get_variable(reference_variable) is not None
                }
                requested_variables.update(
                    variable_references.difference(required_variables)
                )

        return self.exclude_fake_dimensions(required_variables)

    def get_required_dimensions(self, variables: Set[str]) -> Set[str]:
        """ Return a single set of all variables that are used as dimensions
            for any of the listed variables.

        """
        return set(dimension
                   for variable in variables
                   for dimension
                   in getattr(self.get_variable(variable), 'dimensions', [])
                   if self.get_variable(dimension) is not None)

    def get_spatial_dimensions(self, variables: Set[str]) -> Set[str]:
        """ Return a single set of all variables that are both used as
            dimensions for any of the input variables, and that are geographic
            in nature (as determined by the `units` metadata attribute).

            Not all variables have dimensions, which necessitates a check on
            their existence before determining the dimension is geographic.

        """
        return set(dimension
                   for dimension
                   in self.get_required_dimensions(variables)
                   if self.get_variable(dimension).is_geographic())

    def get_temporal_dimensions(self, variables: Set[str]) -> Set[str]:
        """ Return a single set of all variables that are both used as
            dimensions for any of the input variables, and that are temporal
            in nature (as determined by the `units` metadata attribute).

            Not all variables have dimensions, which necessitates a check on
            their existence before determining the dimension is temporal.

        """
        return set(dimension
                   for dimension
                   in self.get_required_dimensions(variables)
                   if self.get_variable(dimension).is_temporal())
                   
    @staticmethod
    def exclude_fake_dimensions(variable_set: Set[str]) -> Set[str]:
        """ An OPeNDAP `.dmr` can contain fake dimensions, used to supplement
            missing information for a granule. These cannot be retrieved when
            requesting a subset from an OPeNDAP server, and must be removed
            from the list of required variables.

        """
        fakedim_pattern = re.compile(r'.*/FakeDim\d+')

        return {variable for variable in variable_set
                if not fakedim_pattern.match(variable)}


class VarInfoFromDmr(VarInfoBase):
    """ A child class that inherits from `VarInfoBase` and implements functions
        to retrieve a dataset from a `.dmr` file, and the extract variables
        from the resulting XML tree.

    """
    def _read_dataset(self, file_path: str):
        """ Extract the XML tree and namespace from an OPeNDAP `.dmr` file. """
        with open(file_path, 'r') as file_handler:
            dmr_content = file_handler.read()

        self.dataset = ET.fromstring(dmr_content)
        self.namespace = get_xml_namespace(self.dataset)

    def _set_global_attributes(self):
        """ Extract all global attributes from a `.dmr` file. First this method
            searches for a root level Attribute element with name
            "HDF5_GLOBAL". If this is present, it is assumed to be a container
            for the global attributes. If "HDF5_GLOBAL" is absent, the global
            attributes are assumed to be direct children of the root Dataset
            element in the XML tree. All child Attribute elements children with
            a type property corresponding to a DAP4 variable type are placed in
            an output dictionary. If the type is not recognised by the DAP4
            protocol, the attribute is assumed to be a string.

        """
        def save_attribute(output, group_path, attribute):
            attribute_name = attribute.get('name')
            dap4_type = attribute.get('type')

            if dap4_type != 'container':
                attribute_value = attribute.find(f'{self.namespace}Value').text
                numpy_type = DAP4_TO_NUMPY_MAP.get(dap4_type, str)

                group_dictionary = output

                if group_path != '':
                    # Recurse through group keys to retrieve the nested group
                    # to which the attribute belongs. If a group in the path
                    # doesn't exist, because this attribute is the first to be
                    # parsed from this group, then create a new nested
                    # dictionary for the group to contain the child attributes
                    nested_groups = group_path.lstrip('/').split('/')
                    for group in nested_groups:
                        group_dictionary = group_dictionary.setdefault(group, {})

                group_dictionary[attribute_name] = numpy_type(attribute_value)

        globals_parent = self.dataset.find(
            f'{self.namespace}Attribute[@name="HDF5_GLOBAL"]'
        ) or self.dataset

        self.traverse_elements(globals_parent, {'Attribute'}, save_attribute,
                               self.global_attributes)

    def _extract_variables(self):
        """ Iterate through all children of the `.dmr` root dataset element.
            If the child matches one of the DAP4 variable types, then create an
            instance of the `VariableFromDmr` class, and assign it to either
            the `variables_with_coordinates` or the `metadata_variables`
            dictionary accordingly.

        """
        def save_variable(output, group_path, element):
            element_path = '/'.join([group_path, element.get('name')])
            variable = VariableFromDmr(element, self.cf_config,
                                       namespace=self.namespace,
                                       full_name_path=element_path)
            output[variable.full_name_path] = variable
            self._assign_variable(variable)

        all_variables = {}

        self.traverse_elements(self.dataset, set(DAP4_TO_NUMPY_MAP.keys()),
                               save_variable, all_variables)

        self._remove_non_variable_references()

    def _remove_non_variable_references(self):
        """ After all references have been combined, remove those that point to
            non-existent variables. For example dimensions that are present in
            a variable to only denote array size in that dimension. This must
            be done after all variables are parsed, to ensure a reference isn't
            being made to a variable that hasn't yet been extracted.

        """
        self.references = {reference
                           for reference
                           in self.references
                           if self.get_variable(reference) is not None}

    def traverse_elements(self, element: ET.Element, element_types: Set[str],
                          operation, output, group_path: str = ''):
        """ Perform a depth first search of the `.dmr` `Dataset` element.
            When a variable is located perform an operation on the supplied
            output object, using the supplied function or class.

        """
        for child in list(element):
            # If it is in the DAP4 list: use the function
            # else, if it is a Group, call this function again
            element_type = child.tag.replace(self.namespace, '')

            if element_type in element_types:
                operation(output, group_path, child)
            elif element_type == 'Group':
                new_group_path = '/'.join([group_path, child.get('name')])
                self.traverse_elements(child, element_types, operation, output,
                                       new_group_path)


class VarInfoFromNetCDF4(VarInfoBase):
    """ A child class that inherits from `VarInfoBase` and implements functions
        to retrieve a dataset from a NetCDF-4 file, and extract the variables
        by traversing the granule structure.

    """
    def _read_dataset(self, file_path: str):
        """ Set the dataset to the file path for the NetCDF-4 file. This is
            done instead of assigning a `netCDF4.Dataset` instance, so that the
            file is not still in memory after being parsed, so that other
            services can interact with the NetCDF-4 file without any conflicts.

        """
        self.dataset = file_path

    def _set_global_attributes(self):
        """ Extract all global attributes from the NetCDF-4 dataset. Using the
            `Dataset.__dict__` method allows extraction of all global
            attributes in a single call.

        """
        with Dataset(self.dataset, 'r') as dataset:
            self.global_attributes = dataset.__dict__

    def _extract_variables(self):
        """ Traverse all groups of the NetCDF-4 file, beginning at the  root
            group.

        """
        with Dataset(self.dataset, 'r') as dataset:
            self._parse_group(dataset)

    def _parse_group(self, group: Union[Dataset, Group]):
        """ If the child matches one of the DAP4 variable types, then create an
            instance of the `VariableFromDmr` class, and assign it to either
            the `variables_with_coordinates` or the `metadata_variables`
            dictionary accordingly.

        """
        for netcdf4_variable in group.variables.values():
            variable_path = '/'.join([group.path, netcdf4_variable.name])
            variable_path = f'/{variable_path.lstrip("/")}'

            variable = VariableFromNetCDF4(netcdf4_variable, self.cf_config,
                                           namespace=self.namespace,
                                           full_name_path=variable_path)

            self._assign_variable(variable)

        for child_group in group.groups.values():
            self._parse_group(child_group)
