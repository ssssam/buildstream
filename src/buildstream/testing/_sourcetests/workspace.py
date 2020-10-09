#
#  Copyright (C) 2018 Codethink Limited
#  Copyright (C) 2019 Bloomberg Finance LP
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#

# Pylint doesn't play well with fixtures and dependency injection from pytest
# pylint: disable=redefined-outer-name

import os
import pytest

from buildstream import _yaml
from .. import create_repo
from .. import cli  # pylint: disable=unused-import
from .utils import kind  # pylint: disable=unused-import

# Project directory
TOP_DIR = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = os.path.join(TOP_DIR, "project")


@pytest.mark.datafiles(DATA_DIR)
def test_open(cli, tmpdir, datafiles, kind):
    project_path = str(datafiles)
    bin_files_path = os.path.join(project_path, "files", "bin-files")

    element_name = "workspace-test-{}.bst".format(kind)
    element_path = os.path.join(project_path, "elements")

    # Create our repo object of the given source type with
    # the bin files, and then collect the initial ref.
    repo = create_repo(kind, str(tmpdir))
    ref = repo.create(bin_files_path)

    # Write out our test target
    element = {"kind": "import", "sources": [repo.source_config(ref=ref)]}
    _yaml.roundtrip_dump(element, os.path.join(element_path, element_name))

    # Assert that there is no reference, a fetch is needed
    assert cli.get_element_state(project_path, element_name) == "fetch needed"

    workspace_cmd = os.path.join(project_path, "workspace_cmd")
    os.makedirs(workspace_cmd, exist_ok=True)
    # remove the '.bst' at the end of the element
    workspace_dir = os.path.join(workspace_cmd, element_name[-4:])

    # Now open the workspace, this should have the effect of automatically
    # fetching the source from the repo.
    args = ["workspace", "open"]
    args.extend(["--directory", workspace_dir])

    args.append(element_name)
    result = cli.run(cwd=workspace_cmd, project=project_path, args=args)

    result.assert_success()

    # Assert that we are now buildable because the source is now cached.
    assert cli.get_element_state(project_path, element_name) == "buildable"

    # Check that the executable hello file is found in each workspace
    filename = os.path.join(workspace_dir, "usr", "bin", "hello")
    assert os.path.exists(filename)
