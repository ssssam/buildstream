#!/usr/bin/env python3
#
#  Copyright (C) 2016 Codethink Limited
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Tristan Van Berkom <tristan.vanberkom@codethink.co.uk>

import os
import copy
import collections

from . import LoadError, LoadErrorReason
from . import _yaml
from ._yaml import CompositePolicy, CompositeTypeError, CompositeOverrideError

from ._metaelement import MetaElement
from ._metasource import MetaSource


#################################################
#                 Local Types                   #
#################################################
#
# List of symbols we recognize
#
class Symbol():
    FILENAME = "filename"
    KIND = "kind"
    DEPENDS = "depends"
    VARIANT = "variant"
    VARIANTS = "variants"
    ARCHES = "arches"
    SOURCES = "sources"
    CONFIG = "config"
    VARIABLES = "variables"
    ENVIRONMENT = "environment"
    TYPE = "type"
    BUILD = "build"
    RUNTIME = "runtime"
    DIRECTORY = "directory"


# A simple dependency object
#
class Dependency():
    def __init__(self, owner_name, name, variant_name=None, filename=None, dep_type=None):
        self.owner = owner_name
        self.name = name
        self.variant_name = variant_name
        self.filename = filename
        self.dep_type = dep_type


# Holds a variant dictionary and normalized Dependency list
# for later compositing, after resolving which variants to choose
#
class Variant():
    def __init__(self, owner, data):
        self.data = data
        self.name = _yaml.node_get(self.data, str, Symbol.VARIANT)
        self.dependencies = extract_depends_from_node(owner, self.data)

        del self.data[Symbol.VARIANT]


# A utility object wrapping the LoadElement, this represents
# a hypothetical configuration of an element, it describes:
#
#  o The dependency pulling in the element
#  o The chosen variant
#  o The dependencies the element has when configured for the given variant
#
class LoadElementConfig():
    def __init__(self, dependency, element, variant_name=None):
        self.dependency = dependency
        self.element = element
        self.filename = element.filename
        self.variant_name = variant_name
        self.deps = element.deps_for_variant(variant_name)


# VariantError is raised to indicate that 2 elements
# depend on a given element in a way that conflicts
#
class VariantError(Exception):
    def __init__(self, element_config, dependency):
        super(VariantError, self).__init__(
            "Variant disagreement occurred.\n"
            "Element '%s' requested element '%s (%s)'\n"
            "Element '%s' requested element '%s (%s)" %
            (element_config.dependency.owner, element_config.filename,
             element_config.dependency.variant_name,
             dependency.owner, element_config.filename,
             dependency.variant_name))


# A transient object breaking down what is loaded
# allowing us to do complex operations in multiple
# passes
#
class LoadElement():

    def __init__(self, data, filename, basedir, arch, elements):

        self.filename = filename
        self.data = data
        self.arch = arch
        self.name = element_name_from_filename(filename)
        self.elements = elements

        # These are shared with the owning Loader object
        self.basedir = basedir

        # Process arch conditionals
        self.process_arch(self.data)

        # Dependency objects after resolving variants
        self.variant_name = None
        self.deps = []

        # Base dependencies
        self.base_deps = extract_depends_from_node(self.name, self.data)

        # Load the Variants
        self.variants = []
        variants_node = _yaml.node_get(self.data, list, Symbol.VARIANTS, default_value=[])
        for variant_node in variants_node:
            index = variants_node.index(variant_node)
            variant_node = _yaml.node_get(self.data, dict, Symbol.VARIANTS, indices=[index])
            variant = Variant(self.name, variant_node)

            # Process arch conditionals on individual variants
            self.process_arch(variant.data)
            self.variants.append(variant)

        if len(self.variants) == 1:
            provenance = _yaml.node_get_provenance(self.data, key=Symbol.VARIANTS)
            raise LoadError(LoadErrorReason.INVALID_DATA,
                            "%s: Only one variant declared, an element "
                            "declaring variants must declare at least two variants" %
                            str(provenance))

        # Strip em from the data now
        del self.data[Symbol.VARIANTS]

    #############################################
    #        Routines used by the Loader        #
    #############################################

    # Checks if this element depends on another element, directly
    # or indirectly. This does NOT follow variants and is only
    # useful after variants are resolved.
    #
    def depends(self, other):
        for dep in self.deps:
            elt = self.elements[dep.name]
            if elt == other:
                return True
            elif elt.depends(other):
                return True
        return False

    # Fetch a Variant by name
    #
    def lookup_variant(self, variant_name):
        for variant in self.variants:
            if variant.name == variant_name:
                return variant

    # deps_for_variant()
    #
    # Fetches the set of Dependency objects for a given variant name
    #
    def deps_for_variant(self, variant_name):
        deps = copy.copy(self.base_deps)

        variant = None
        if variant_name:
            variant = self.lookup_variant(variant_name)

        # If the Dependency is already mentioned in the base dependencies
        # a variant may modify it by overriding the dependency variant
        if variant:

            for variant_dep in variant.dependencies:
                override = False
                for dep in deps:
                    if dep.filename == variant_dep.filename:
                        index = deps.index(dep)
                        deps[index] = variant_dep
                        override = True
                        break

                # Dependency not already declared, append new one
                if not override:
                    deps.append(variant_dep)

        # Return the list of dependencies for this variant
        return deps

    # Apply the chosen variant into the element data
    #
    def apply_element_config(self, element_config):

        # Save the final decision on Dependencies
        self.element_config = element_config
        self.variant_name = element_config.variant_name
        self.deps = element_config.deps

        variant = None
        if self.variant_name:
            variant = self.lookup_variant(self.variant_name)

        if variant:
            provenance = _yaml.node_get_provenance(variant.data)

            # Composite anything from the variant data into the element data
            #
            # Possibly this should not be typesafe, since branch names can
            # possibly be strings or interpreted by YAML as integers (for
            # numeric branch names)
            #
            try:
                _yaml.composite_dict(self.data, variant.data,
                                     policy=CompositePolicy.ARRAY_APPEND,
                                     typesafe=True)
            except CompositeTypeError as e:
                raise LoadError(
                    LoadErrorReason.ILLEGAL_COMPOSITE,
                    "%s: Variant '%s' specifies type '%s' for path '%s', expected '%s'" %
                    (str(provenance),
                     element_config.variant_name,
                     e.actual_type.__name__, e.path,
                     e.expected_type.__name__)) from e

    #############################################
    #        Internal to the LoadElement        #
    #############################################
    def process_arch(self, data):

        arches = _yaml.node_get(data, dict, Symbol.ARCHES, default_value={})
        arch = {}
        if arches:
            arch = _yaml.node_get(arches, dict, self.arch, default_value={})

        if arch:
            try:
                _yaml.composite_dict(data, arch,
                                     policy=CompositePolicy.ARRAY_APPEND,
                                     typesafe=True)
            except CompositeTypeError as e:
                provenance = _yaml.node_get_provenance(arch, key=self.arch)
                raise LoadError(LoadErrorReason.ILLEGAL_COMPOSITE,
                                "%s: Arch %s specifies type '%s' for path '%s', expected '%s'" %
                                (str(provenance), self.arch,
                                 e.actual_type.__name__,
                                 e.path,
                                 e.expected_type.__name__)) from e

        del self.data[Symbol.ARCHES]


# Creates an array of dependency dicts from a given dict node 'data',
# allows both strings and dicts for expressing the dependency and
# throws a comprehensive LoadError in the case that the data is malformed.
#
# After extracting depends, they are removed from the data node
#
# Returns a normalized array of Dependency objects
def extract_depends_from_node(owner, data):
    depends = _yaml.node_get(data, list, Symbol.DEPENDS, default_value=[])
    output_deps = []

    for dep in depends:

        if isinstance(dep, str):
            dependency = Dependency(owner, element_name_from_filename(dep), filename=dep)

        elif isinstance(dep, dict):
            # Make variant optional, for this we set it to None after
            variant = _yaml.node_get(dep, str, Symbol.VARIANT, default_value="")
            if not variant:
                variant = None

            # Make type optional, for this we set it to None after
            dep_type = _yaml.node_get(dep, str, Symbol.TYPE, default_value="")
            if not dep_type:
                dep_type = None
            elif dep_type not in [Symbol.BUILD, Symbol.RUNTIME]:
                provenance = _yaml.node_get_provenance(dep, key=Symbol.TYPE)
                raise LoadError(LoadErrorReason.INVALID_DATA,
                                "%s: Dependency type '%s' is not 'build' or 'runtime'" %
                                (str(provenance), dep_type))

            filename = _yaml.node_get(dep, str, Symbol.FILENAME)
            name = element_name_from_filename(filename)
            dependency = Dependency(owner, name, variant_name=variant, filename=filename, dep_type=dep_type)

        else:
            index = depends.index(dep)
            provenance = _yaml.node_get_provenance(data, key=Symbol.DEPENDS, indices=[index])

            raise LoadError(LoadErrorReason.INVALID_DATA,
                            "%s: List '%s' element %d is not a list or dict" %
                            (str(provenance), Symbol.DEPENDS, index))

        output_deps.append(dependency)

    # Now delete "depends", we dont want it anymore
    del data[Symbol.DEPENDS]

    return output_deps


def element_name_from_filename(filename):
    element_basename = os.path.basename(filename)
    element_name = os.path.splitext(element_basename)[0]
    return element_name


#################################################
#                   The Loader                  #
#################################################
#
# The Loader class does the heavy lifting of parsing a target
# bst file and creating a tree of LoadElements
#
class Loader():

    def __init__(self, basedir, filename, variant, arch):

        # Ensure we have an absolute path for the base directory
        #
        if not os.path.isabs(basedir):
            basedir = os.path.abspath(basedir)

        if os.path.isabs(filename):
            # XXX Should this just be an assertion ?
            # Expect that the caller gives us the right thing at least ?
            raise LoadError(LoadErrorReason.INVALID_DATA,
                            "Target '%s' was not specified as a relative "
                            "path to the base project directory: %s" %
                            (filename, basedir))

        # Base project directory
        self.basedir = basedir

        # Target bst filename
        self.target_filename = filename
        self.target = element_name_from_filename(filename)

        # Optional variant
        self.target_variant = variant

        # Build architecture
        self.arch = arch

        self.loaded_files = {}   # Table of files we've already loaded
        self.meta_elements = {}  # Dict of resolved meta elements by name
        self.elements = {}       # Dict of elements

    ########################################
    #           Main Entry Point           #
    ########################################

    # load():
    #
    # Loads the project based on the parameters given to the constructor
    #
    # Raises: LoadError
    #
    # Returns: The toplevel LoadElement
    def load(self):

        # First pass, recursively load files and populate our table of LoadElements
        #
        self.load_file(self.target_filename)

        #
        # Deal with variants
        #
        self.resolve_variants()

        # Finally, wrap what we have into LoadElements and return the target
        #
        return self.collect_element(self.target)

    ########################################
    #             Loading Files            #
    ########################################

    # Recursively load bst files
    #
    def load_file(self, filename):

        # Silently ignore already loaded files
        if filename in self.loaded_files:
            return
        self.loaded_files[filename] = True

        # Raise error if two files claim the same name
        element_name = element_name_from_filename(filename)
        if element_name in self.elements:
            element = self.elements[element_name]
            raise LoadError(LoadErrorReason.INVALID_DATA,
                            "Tried to load file '%s' but existing file '%s' has the same name" %
                            (filename, element.filename))

        fullpath = os.path.join(self.basedir, filename)

        # Load the element and track it in our elements table
        data = _yaml.load(fullpath, filename)
        element = LoadElement(data, filename, self.basedir, self.arch, self.elements)

        self.elements[element_name] = element

        # Load all possible dependency files for the new LoadElement
        for dep in element.base_deps:
            self.load_file(dep.filename)

        for variant in element.variants:
            for dep in variant.dependencies:
                self.load_file(dep.filename)

    ########################################
    #          Resolving Variants          #
    ########################################
    #
    # The first rule of variants is that for any given element provided by
    # itself as a pipeline target, all variants of that element must be
    # buildable and not present any variant conflict.
    #
    # However, any variant of a given element that is not the target may
    # end up being built differently - this is because siblings in the pipeline
    # may prefer a variant of a dependency for which the given element's
    # dependency was ambivalent.
    #
    # Considering that variants can effect what and how an element depends
    # on other elements; resolving the variants is a trial and error activity,
    # even if this is not true for the target element (as stated as the first
    # rule), it is true of every other element in a given pipeline.
    #
    # As such, resolving the variants is a recursive process of trial and error
    #
    #  1.) Construct a "variant tree"
    #
    #      The variant tree is a tree of elements dicts, these refer to the
    #      element filename and contain an array of variants; each member of
    #      the variant array holds an array of the dependencies which would be
    #      chosen if the given variant of the given element were chosen.
    #
    #  2.) Starting at the top level, try to resolve the
    #
    #      For each element; collect an array of it's variants; each member of
    #      the variant array speaks for the dependencies of the given element
    #
    def resolve_variants(self):
        target_variant = self.target_variant
        target_element = self.elements[self.target]

        # If a target was not specified, this is an explicit request for the
        # first variant
        if not target_variant and target_element.variants:
            target_variant = target_element.variants[0].name

        # Recurse until the cows come home !
        #
        toplevel_config = LoadElementConfig(None, target_element, target_variant)
        try:
            pool = self.configure_variants(toplevel_config, [])
        except VariantError as e:
            raise LoadError(LoadErrorReason.VARIANT_DISAGREEMENT, str(e)) from e

        # Now apply the chosen variant configurations
        #
        for element_config in pool:
            element_config.element.apply_element_config(element_config)

    #
    # configure_variants()
    #
    # Args:
    #   element_config (LoadElementConfig): the element to try
    #   pool (list): A list of LoadElementConfig objects
    #
    # Returns:
    #   A new configuration
    #
    # With a given configuration in context, reports whether the configuration
    # is a valid one for the given element and all of the possible elements on
    # which this element depends, returning a new configuration comprised of
    # the given configuration and the first valid configuration of it's
    # dependencies
    #
    def configure_variants(self, element_config, pool):

        # First, check the new element configuration to try against
        # the existing ones in the pool for conflicts.
        #
        for config in pool:

            # The configuration pool can have only one selected configuration
            # for each element, handle intersections and conflicts.
            #
            if config.element is element_config.element:
                if config.variant_name == element_config.variant_name:
                    # A path converges on the same element configuration,
                    # this iteration can be safely discarded.
                    return pool
                else:
                    # Two different variants of the same element should be reached
                    # on a path of variant agreement.
                    raise VariantError(element_config, config.dependency)

        # Now add ourselves to the pool and recurse into the dependency list
        new_pool = pool + [element_config]
        return self.configure_dependency_variants(element_config.deps, new_pool)

    def configure_dependency_variants(self, deps, pool):

        # This is just the end of the list
        if not deps:
            return pool

        # Loop over the possible variants for this dependency
        dependency = deps[0]
        element = self.elements[dependency.name]

        # First create one list of element configurations to try, one for
        # each possible variant under this element configuration
        #
        element_configs_to_try = []
        if dependency.variant_name:
            config = LoadElementConfig(dependency, element, dependency.variant_name)
            element_configs_to_try.append(config)
        elif len(element.variants) == 0:
            config = LoadElementConfig(dependency, element, None)
            element_configs_to_try.append(config)
        else:
            for variant in element.variants:
                config = LoadElementConfig(dependency, element, variant.name)
                element_configs_to_try.append(config)

        # Loop over every possible element configuration for this dependency
        #
        accum_pool = None
        last_error = None

        for element_config in element_configs_to_try:

            # Reset the attempted new pool for each try
            accum_pool = None

            try:
                # If this configuration of the this element succeeds...
                try_pool = self.configure_variants(element_config, pool)

                # ... Then recurse into sibling elements
                accum_pool = self.configure_dependency_variants(deps[1:], try_pool)

            except VariantError as e:

                # Hold onto the error
                last_error = e

                # If this element configuration failed, then find more possible
                # element configurations
                continue

        # If unable to find any valid configuration, raise a VariantError
        if not accum_pool:
            raise last_error

        return accum_pool

    ########################################
    #          Element Collection          #
    ########################################

    # Collect the toplevel elements we have, resolve their deps and return !
    #
    def collect_element(self, element_name):

        element = self.elements[element_name]

        # Return the already built one, if we already built it
        meta_element = self.meta_elements.get(element_name)
        if meta_element:
            return meta_element

        data = copy.deepcopy(element.data)

        meta_sources = []

        sources = _yaml.node_get(data, list, Symbol.SOURCES, default_value=[])
        for source in sources:
            provenance = _yaml.node_get_provenance(source)
            kind = _yaml.node_get(source, str, Symbol.KIND)
            del source[Symbol.KIND]

            # Directory is optional
            directory = _yaml.node_get(source, str, Symbol.DIRECTORY, default_value='')
            if directory:
                del source[Symbol.DIRECTORY]
            else:
                directory = None

            meta_source = MetaSource(kind, source, directory,
                                     provenance.node,
                                     provenance.toplevel,
                                     provenance.filename)
            meta_sources.append(meta_source)

        meta_element = MetaElement(element_name, data.get('kind'), meta_sources,
                                   data.get(Symbol.CONFIG, {}),
                                   data.get(Symbol.VARIABLES, {}),
                                   data.get(Symbol.ENVIRONMENT, {}))

        # Check circular dependencies, if we're adding something
        # which depends on something already there, it's a circular dep
        for elt_name, _ in self.meta_elements.items():
            elt = self.elements[elt_name]

            # XXX FIXME: This is horribly expensive
            if element.depends(elt) and elt.depends(element):
                raise LoadError(LoadErrorReason.CIRCULAR_DEPENDENCY,
                                "Circular dependency detected for element: %s" %
                                element.filename)

        # Cache it now, make sure it's already there before recursing
        self.meta_elements[element_name] = meta_element

        # Sort dependencies once per element at load time
        sorted_deps = sorted(element.deps, key=lambda dep: dep.name)
        for dep in sorted_deps:
            meta_dep = self.collect_element(dep.name)
            if dep.dep_type != 'runtime':
                meta_element.build_dependencies.append(meta_dep)
            if dep.dep_type != 'build':
                meta_element.dependencies.append(meta_dep)

        return meta_element
