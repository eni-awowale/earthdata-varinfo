""" This module contains a class designed to read information from a `.dmr`
    file. This should group the input into science variables, metadata,
    coordinates, dimensions and ancillary data sets.

"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Set, Tuple, Union
import re
import xml.etree.ElementTree as ET

from netCDF4 import Variable as NetCDF4Variable

from varinfo.cf_config import CFConfig
from varinfo.utilities import CF_REFERENCE_ATTRIBUTES, get_xml_attribute


InputVariableType = Union[ET.Element, NetCDF4Variable]


class VariableBase(ABC):
    """ A class to represent a single variable contained within a granule
        representation. It will produce an object in which references are
        fully qualified, and also augmented by any overrides or supplements
        from the supplied configuration file.

    """
    def __init__(self, variable: InputVariableType, cf_config: CFConfig,
                 namespace: str, full_name_path: str):
        """ Extract the references contained within the appropriate
            CF-Convention attributes of the variable. These should be augmented
            by information from the `CFConfig` instance passed to the class.

            Additionally, store all metadata attributes in a dictionary.

        """
        self.namespace = namespace
        self.full_name_path = full_name_path
        self.cf_config = cf_config.get_cf_attributes(self.full_name_path)
        self.group_path, self.name = self._extract_group_and_name()
        self.data_type = self._get_data_type(variable)
        self.attributes = self._get_attributes(variable)
        self.references = self._get_all_cf_references()
        self.dimensions = self._extract_dimensions(variable)

    @abstractmethod
    def _get_data_type(self, variable: InputVariableType):
        """ Extract a string representation of the variable data type. """

    @abstractmethod
    def _get_raw_dimensions(self, variable: InputVariableType):
        """ Retrieve the dimension names as they are stored within the
            variable.

        """

    @abstractmethod
    def _get_attributes(self, variable: InputVariableType) -> Dict[str, str]:
        """ Extract all attributes for the variable. The contents of the
            output dictionary will be as stored in the granule metadata, with
            no augmentation from `CFConfig`. For variables references contained
            in CF-Convention attributes, users should retrieve values from the
            self.references dictionary.

        """

    def get_range(self) -> Optional[List[float]]:
        """ Retrieve the range of valid data from the variable metadata. First,
            try to parse the `valid_range` metadata attribute. If this is
            absent, check for a combination of `valid_min` and `valid_max`.

            If insufficient range information is present in the metadata, this
            method will return `None`.

        """
        valid_range = self.attributes.get('valid_range')

        if valid_range is None:
            valid_min = self.attributes.get('valid_min')
            valid_max = self.attributes.get('valid_max')

            if valid_min is not None and valid_max is not None:
                valid_range = [valid_min, valid_max]

        return valid_range

    def get_valid_min(self) -> Optional[float]:
        """ Retrieve the minimum valid value for variable data from the
            associated metadata. First try to retrieve data from the
            `valid_min` metadata attribute. If this is absent, then try to
            retrieve the same information from the `valid_range` metadata.

            If insufficient range information is present in the metadata, this
            method will return `None`.

        """
        valid_min = self.attributes.get('valid_min')

        if valid_min is None:
            valid_range = self.attributes.get('valid_range')
            if isinstance(valid_range, list) and len(valid_range) == 2:
                valid_min = valid_range[0]

        return valid_min

    def get_valid_max(self) -> Optional[float]:
        """ Retrieve the maximum valid value for variable data from the
            associated metadata. First try to retrieve data from the
            `valid_max` metadata attribute. If this is absent, then try to
            retrieve the same information from the `valid_range` metadata.

            If insufficient range information is present in the metadata, this
            method will return `None`.

        """
        valid_max = self.attributes.get('valid_max')

        if valid_max is None:
            valid_range = self.attributes.get('valid_range')
            if isinstance(valid_range, list) and len(valid_range) == 2:
                valid_max = valid_range[1]

        return valid_max

    def get_references(self) -> Set[str]:
        """ Combine the references extracted from the ancillary_variables,
            coordinates and dimensions data into a single set for VarInfo to
            use directly.

            The variable dimensions are cast as a set to allow combination with
            the other set attributes of the `VariableBase` class. The
            dimensions attribute is kept as a list prior to combination in the
            full set of variable references to ensure that the ordering of the
            dimensions is preserved.

        """
        return set(self.dimensions).union(*self.references.values())

    def is_geographic(self) -> bool:
        """ Use heuristics to determine if the variable is a geographic
            coordinate based on its units. A latitude variable will have units
            'degrees_north' and a longitude variable with have units
            'degrees_east'.

        """
        return self.is_longitude() or self.is_latitude()

    def is_latitude(self) -> bool:
        """ Determine if the variable is a latitude based on the `units`
            metadata attribute being 'degrees_north' or other similar options
            as defined in section 4.1 of the CF Conventions (v1.8).

        """
        return self.attributes.get('units') in ['degrees_north',
                                                'degree_north',
                                                'degrees_N', 'degree_N',
                                                'degreesN', 'degreeN']

    def is_longitude(self) -> bool:
        """ Determine if the variable is a longitude based on the `units`
            metadata attribute being 'degrees_east' or other similar options
            as defined in section 4.2 of the CF Conventions (v1.8).

        """
        return self.attributes.get('units') in ['degrees_east', 'degree_east',
                                                'degrees_E', 'degree_E',
                                                'degreesE', 'degreeE']

    def is_temporal(self) -> bool:
        """ Determine if the variable is a time based on the `units`
            metadata attribute being 'since' or other similar options
            as defined in section 4.1 of the CF Conventions (v1.8).

        """
        return " since " in self.attributes.get('units') 

    def _get_all_cf_references(self) -> Dict[str, Set[str]]:
        """ Retrieve a dictionary containing all CF-Convention attributes
            within the variable that have references to other variables in the
            granule. These variable references will be fully qualified paths.

        """
        return {attribute: self._get_cf_references(attribute)
                for attribute in CF_REFERENCE_ATTRIBUTES
                if len(self._get_cf_references(attribute)) != 0}

    def _get_cf_references(self, attribute_name: str) -> Set[str]:
        """ Retrieve an attribute from the parsed varaible metadata, correcting
            for any known artefacts (missing or incorrect references). Then
            split this string and qualify the individual references.

        """
        attribute_string = self._get_cf_attribute(attribute_name)
        return self._extract_references(attribute_string)

    def _get_cf_attribute(self, attribute_name: str) -> Optional[str]:
        """ Retrieve an attribute from the parsed variable metadata. Then check
            the output from the CF configuration file, to see if this value
            should be replaced, or supplemented with more data.

        """
        cf_overrides = self.cf_config['cf_overrides'].get(attribute_name)
        cf_supplements = self.cf_config['cf_supplements'].get(attribute_name)

        if cf_overrides is not None:
            attribute_value = cf_overrides
        else:
            attribute_value = self.attributes.get(attribute_name)

        if cf_supplements is not None and attribute_value is not None:
            attribute_value += f', {cf_supplements}'
        elif cf_supplements is not None:
            attribute_value = cf_supplements

        return attribute_value

    def _extract_references(self, attribute_string: str) -> Set[str]:
        """ Given a string value of an attribute, which may contain multiple
            references to dataset, split that string based on either commas,
            or spaces (or both together). Then if any reference is a relative
            path, make it absolute.

        """
        if attribute_string is not None:
            raw_references = re.split(r'\s+|,\s*', attribute_string)
            references = set(self._qualify_references(raw_references))
        else:
            references = set()

        return references

    def _extract_dimensions(self, variable: ET.Element) -> List[str]:
        """ Find the dimensions for the variable in question. If there are
            overriding or supplemental dimensions from the CF configuration
            file, these are used instead of, or in addition to, the raw
            dimensions from the `.dmr`. All references are converted to
            absolute paths in the granule. A set of all fully qualified
            references is returned.

        """
        overrides = self.cf_config['cf_overrides'].get('dimensions')
        supplements = self.cf_config['cf_supplements'].get('dimensions')

        if overrides is not None:
            dimensions = re.split(r'\s+|,\s*', overrides)
        else:
            dimensions = [dimension
                          for dimension in self._get_raw_dimensions(variable)
                          if dimension is not None]

        if supplements is not None:
            dimensions += re.split(r'\s+|,\s*', supplements)

        return self._qualify_references(dimensions)

    def _qualify_references(self, raw_references: List[str]) -> List[str]:
        """ Take a list of local references to other variables, and produce a
            list of absolute references.

        """
        references = []

        if self.group_path is not None:
            for reference in raw_references:
                if reference.startswith('../'):
                    # Reference is relative, and requires manipulation
                    absolute_path = self._construct_absolute_path(reference)
                elif reference.startswith('/'):
                    # Reference is already absolute
                    absolute_path = reference
                elif reference.startswith('./'):
                    # Reference is in the same group as this variable
                    absolute_path = self.group_path + reference[1:]
                else:
                    # Reference is in the same group as this variable
                    absolute_path = '/'.join([self.group_path, reference])

                references.append(absolute_path)

        else:
            for reference in raw_references:
                if reference.startswith('/'):
                    absolute_path = reference
                else:
                    absolute_path = f'/{reference}'

                references.append(absolute_path)

        return references

    def _construct_absolute_path(self, reference: str) -> str:
        """ For a relative reference to another variable (e.g. '../latitude'),
            construct an absolute path by combining the reference with the
            group path of the variable.

        """
        relative_prefix = '../'
        group_path_pieces = self.group_path.split('/')

        while reference.startswith(relative_prefix):
            reference = reference[len(relative_prefix):]
            group_path_pieces.pop()

        absolute_path = group_path_pieces + [reference]
        return '/'.join(absolute_path)

    def _extract_group_and_name(self) -> Tuple[str]:
        """ Extract the group and base name of a variable from the full path,
            e.g. '/this/is/my/variable' should return a two-element tuple:
            ('/this/is/my', 'variable').

        """
        split_full_path = self.full_name_path.split('/')
        name = split_full_path.pop(-1)
        group_path = '/'.join(split_full_path) or None

        return group_path, name


class VariableFromDmr(VariableBase):
    """ This child class inherits from the `VariableBase` class, and implements
        the abstract methods assuming the variable source is part of an XML
        element tree.

    """
    def _get_data_type(self, variable: ET.Element) -> str:
        """ Extract a string representation of the variable data type. """
        return variable.tag.lstrip(self.namespace).lower()

    def _get_attributes(self, variable: ET.Element) -> Dict:
        """ Locate all child Attribute elements of the variable and extract
            their associated values.

        """
        return {attribute.get('name'): get_xml_attribute(variable,
                                                         attribute.get('name'),
                                                         self.namespace)
                for attribute
                in variable.findall(f'{self.namespace}Attribute')}

    def _get_raw_dimensions(self, variable: ET.Element) -> List[str]:
        """ Extract the raw dimension names from a <Dim /> XML element. """
        return [dimension.get('name')
                for dimension
                in variable.findall(f'{self.namespace}Dim')]


class VariableFromNetCDF4(VariableBase):
    """ This child class inherits from the `VariableBase` class, and implements
        the abstract methods assuming the variable source is part of a NetCDF-4
        file.

    """
    def _get_data_type(self, variable: InputVariableType) -> str:
        """ Extract a string representation of the variable data type. """
        return variable.datatype.name

    def _get_attributes(self, variable: NetCDF4Variable) -> Dict:
        """ Identify all variable attributes and save them to a dictionary. """
        return {attribute_name: getattr(variable, attribute_name, None)
                for attribute_name in variable.ncattrs()}

    def _get_raw_dimensions(self, variable: NetCDF4Variable) -> List[str]:
        """ Retrieve the dimension names as they are stored within the
            variable.

        """
        return list(variable.dimensions)
