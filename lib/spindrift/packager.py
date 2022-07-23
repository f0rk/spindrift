# Copyright 2017-2022, Ryan P. Kelly.

import fnmatch
import io
import glob
import logging
import os.path
import pathlib
import re
import shutil
import subprocess
import tempfile
import warnings
import zipfile

import requests

import spindrift.compat


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


IGNORED = [
    "__pycache__",
    ".git",
    "__pycache__/*",
    ".git/*",
    "*/__pycache__/*",
    "*/.git/*",
]


def package(
        package,
        type,
        entry,
        runtime,
        destination,
        download=True,
        cache_path=None,
        renamed_packages=None,
        prefer_pyc=True,
        boto_handling="default",
        extra_packages=None):
    """Package up the given package.

    :param package: The name of the package to bundle up.
    :param type: The type of package to create. Currently, the only valid
        values are `"plain"`, which is for simple lambda functions written in
        the standard `handler(event, context)` style, and `"flask"` for flask
        applications. Use `"flask-eb"` for elastic beanstalk applications,
        which must be flask applications, and `"flask-eb-reqs"` for elastic
        beanstalk applications, which must be flask applications, which should
        also include a requirements.txt file.
    :param entry: A string describing the entrypoint of the application. For
        type `"plain"` applications, this should import the handler function
        itself and name it `handler`, i.e. `from yourplainapp.handlers import
        snake_handler as handler`.  For `"flask"` applications, this should
        import your application and call it `app`, i.e., `from yourwebapp.app
        import api_app as app`. For `"flask-eb"` or `"flask-eb-reqs"`
        applications, this should import your application and call it
        `application`.
    :param runtime: The runtime to package for. Should be one of `"python2.7"`,
        `"python3.6"`, or `"python3.7"`.
    :param destination: A path on the file system to store the resulting file.
        No parent directories will be created, so you must ensure they exist.
    :param download: When `True`, whether or not to request installable
        manylinux wheels from pypi (default: `True`).
    :param cache_path: A path on the filesystem storing any currently
        downloaded wheels. Wheels will be used if found in cache. Any newly
        downloaded wheels will be stored here. Default of `None` means to use a
        temporary directory.
    :param renamed_packages: Supply a function or a dictionary to rename a
        package. For example, psycopg2 can be replaced with psycopg2-binary by
        supplying a dictionary that maps to the new name. Additionally, mapping
        to None will skip the package altogether.
    :param prefer_pyc: If `True`, remove any .py files that have a corresponding
        .pyc to help save space. If `False`, remove the .pyc file and keep the
        .py (default: `True`).
    :param boto_handling: If `"default"`, use spindrift's default behavior for
        boto/botocore packaging, which is to include them in the output for
        flask-eb or flask-eb-reqs and exclude from the output for other
        packages. If `"include"`, always include.
    :param extra_packages: Extra packages to install, if any.

    """

    dependency_packages = [package]
    if extra_packages is not None:
        dependency_packages.extend(extra_packages)

    dependencies = []

    # determine what our dependencies are
    for dependency_package in dependency_packages:
        dependencies_for_package = find_dependencies(
            type,
            dependency_package,
            renamed_packages,
            boto_handling=boto_handling,
        )

        dependencies.extend(dependencies_for_package)

    dependencies = sorted(list(set(dependencies)))

    # create a temporary directory to start creating things in
    with spindrift.compat.TemporaryDirectory() as temp_path:

        # collect our code...
        populate_directory(
            temp_path,
            package,
            type,
            entry,
            runtime,
            dependencies,
            download=download,
            cache_path=cache_path,
            renamed_packages=renamed_packages,
            prefer_pyc=prefer_pyc,
        )

        # ...and create the archive
        output_archive(temp_path, destination)


def populate_directory(path, package, type, entry, runtime, dependencies, download=True, cache_path=None, renamed_packages=None, prefer_pyc=True):

    logger.info("[{}] populating output directory".format(package))

    # install our dependencies
    installed_dependencies = install_dependencies(
        path,
        package,
        runtime,
        dependencies,
        download=download,
        cache_path=cache_path,
    )

    # install our project itself
    install_project(path, package)

    # prune away any unused files
    prune_python_files(path, prefer_pyc=prefer_pyc)

    # insert our shim
    insert_shim(path, type, entry)

    # write out the requirements.txt file, if applicable
    insert_requirements_txt(path, type, renamed_packages, installed_dependencies)

    logger.info("[{}] done populating output directory".format(package))


def output_archive(path, destination):

    logger.info("outputting archive")

    is_a_file_object = (
        isinstance(destination, io.RawIOBase)
        or isinstance(destination, tempfile._TemporaryFileWrapper)
    )

    # if destination is already a file or file-like object, write to it directly
    if is_a_file_object:

        # create our zip bundle
        create_zip_bundle(path, destination)

    # otherwise, create a temporary file and write it
    else:

        # create a temporary file to zip into
        with tempfile.NamedTemporaryFile(suffix=".zip") as temp_file:

            # create our zip bundle
            create_zip_bundle(path, temp_file.name)

            # output our zip bundle to the given destination
            output_zip_bundle(temp_file.name, destination)

    logger.info("done outputting archive")


def find_dependencies(type, package_name, renamed_packages, boto_handling="default"):
    import pip._vendor.pkg_resources

    if renamed_packages is not None:

        if isinstance(renamed_packages, dict):
            if package_name in renamed_packages:
                package_name = renamed_packages[package_name]
        elif callable(renamed_packages):
            package_name = renamed_packages(package_name)

    if package_name is None:
        return []

    package = pip._vendor.pkg_resources.working_set.by_key[package_name]

    # boto is available on lambda, don't always repackage it
    if package.key in ("boto3", "botocore") and boto_handling == "default":
        if type not in ("flask-eb", "flask-eb-reqs"):
            return []

    ret = [package]

    requires = package.requires()
    for requirement in requires:

        # if this requirement is conditional on the environment, skip it if we
        # don't need it
        if requirement.marker is not None:
            if not requirement.marker.evaluate():
                continue

        ret.extend(
            find_dependencies(
                type,
                requirement.key,
                renamed_packages,
                boto_handling=boto_handling,
            ),
        )

    return sorted(list(set(ret)))


def install_dependencies(path, package, runtime, dependencies, download=True, cache_path=None):

    logger.info("[{}] installing dependencies".format(package))

    # we will return our dependencies, grouped by the method in which they were
    # installed
    installed_dependencies = {}

    # for each dependency
    for dependency in dependencies:

        # don't try to install our own code this way, we'll never need to
        # download or want to override it
        if dependency.key == package:
            continue

        method = install_dependency(
            path,
            package,
            runtime,
            dependency,
            download=download,
            cache_path=cache_path,
        )

        installed_dependencies.setdefault(method, [])
        installed_dependencies[method].append(dependency)

    logger.info("[{}] done installing dependencies".format(package))

    return installed_dependencies


def install_dependency(path, package, runtime, dependency, download=True, cache_path=None):
    logger.info("[{}] installing {}".format(package, dependency.key))
    return _install_dependency(
        path,
        package,
        runtime,
        dependency,
        download=download,
        cache_path=cache_path,
    )


def _install_dependency(path, package, runtime, dependency, download=True, cache_path=None):

    # each of the functions below will return false if they couldn't
    # perform the requested operation, or true if they did. perform the
    # attempts in order, and skip the remaining options if we succeed.

    # see if we've got a manylinux version locally
    rv = install_manylinux_version(
        path,
        dependency,
        runtime,
        cache_path=cache_path,
    )
    if rv:
        logger.info(
            "[{}] installed {} via install_manylinux_version"
            .format(package, dependency.key)
        )

        _mangle_package(path, dependency)
        return "install_manylinux_version"

    # maybe try downloading and installing a manylinux version?
    if download:
        rv = download_and_install_manylinux_version(
            path,
            dependency,
            runtime,
            cache_path=cache_path,
        )
        if rv:
            logger.info(
                "[{}] installed {} via download_and_install_manylinux_version"
                .format(package, dependency.key)
            )

            _mangle_package(path, dependency)
            return "download_and_install_manylinux_version"

    # if we get this far, use whatever package we have installed locally
    rv = install_local_package(path, dependency)
    if rv:

        logger.info(
            "[{}] installed {} via install_local_package"
            .format(package, dependency.key)
        )

        _mangle_package(path, dependency)
        return "install_local_package"

    raise Exception("Unable to find suitable source for {}=={}"
                    .format(dependency.key, dependency.version))


def _mangle_package(path, dependency):

    # some packages just aren't ready to be used when bundled up locally.
    # biggest offenders are packages using pkg_resources.get_distribution.
    # here, we perform any package-specific source modifications.

    if dependency.key == "sqlalchemy-redshift":

        # overwrite __init__.py to replace pkg_resources.get_distribution call
        # with hardcoded version and fix registry entry
        sqlalchemy_redshift_init_path = os.path.join(
            path,
            "sqlalchemy_redshift",
            "__init__.py",
        )

        with open(sqlalchemy_redshift_init_path, "r+") as fp:
            current_init_data = fp.read()

            version_expr = r"get_distribution\('sqlalchemy-redshift'\).version"
            mangled_init_data = re.sub(
                version_expr,
                '"{}"'.format(dependency.version),
                current_init_data,
            )

            register_expr = r"redshift\+psycopg2"
            mangled_init_data = re.sub(
                register_expr,
                "redshift.psycopg2",
                mangled_init_data,
            )

            fp.seek(0)
            fp.truncate()
            fp.write(mangled_init_data)


def install_manylinux_version(path, dependency, runtime, cache_path=None):

    if dependency.key == "cryptography":
        return False

    if cache_path is None:
        cache_path = _get_fake_cache_path()

    # sub out the rest of our work
    rv = _install_cached_manylinux_version(
        cache_path,
        path,
        dependency,
        runtime,
    )

    return rv


def _get_fake_cache_path():
    cache_path = os.path.join(tempfile.gettempdir(), "spindrift_cache")

    if not os.path.exists(cache_path):
        os.makedirs(cache_path)

    return cache_path


def download_and_install_manylinux_version(path, dependency, runtime, cache_path=None):

    if dependency.key == "cryptography":
        return False

    # create our own cache if there is no user specified one
    if cache_path is None:
        cache_path = _get_fake_cache_path()

    # get package info from pypi
    name = dependency.key
    res = requests.get("https://pypi.python.org/pypi/{}/json".format(name))

    # if we don't find the package there, bail
    if res.status_code == 404:
        return False

    # raise for other errors though
    res.raise_for_status()

    # see if we can locate our version in the result
    data = res.json()
    version = dependency.version
    wheel_suffixes = _get_wheel_suffixes(runtime)
    if version not in data["releases"]:
        return False

    # and see if we can find the right wheel
    url = None
    for wheel_suffix in wheel_suffixes:
        for info in data["releases"][version]:
            if info["url"].endswith(wheel_suffix):
                url = info["url"]
                break

        if url is not None:
            break

    # couldn't get the url, bail
    if url is None:
        return False

    # figure out what to save this url as
    wheel_name = os.path.basename(url)
    wheel_path = os.path.join(cache_path, wheel_name)

    # download the discovered url into our ghetto cache
    with open(wheel_path, "wb") as fp:
        res = requests.get(url, stream=True)
        res.raise_for_status()
        for chunk in res.iter_content(chunk_size=1024):
            fp.write(chunk)

    # install the retrieved file
    with zipfile.ZipFile(wheel_path) as zf:
        zf.extractall(path)

    # success
    return True


def _get_wheel_suffixes(runtime):

    if not runtime.startswith("python2.") and not runtime.startswith("python3."):
        raise ValueError(
            "Runtime must start with 'python2.' or 'python3.' (got {!r})"
            .format(runtime)
        )

    available_runtimes = [
        "python2.7",
        "python3.6",
        "python3.7",
        "python3.8",
        "python3.9",
    ]

    if runtime not in available_runtimes:

        available_runtimes_str = ", ".join(available_runtimes)

        warnings.warn(
            "unknown runtime, packaging may fail (only {} are known)."
            "attemping to parse version from runtime value {!r}"
            .format(available_runtimes_str, runtime),
            Warning,
        )

    version = runtime.replace("python", "")
    version = version.replace(".", "")

    suffixes = []

    if runtime.startswith("python2."):
        suffixes.extend([
            "cp{version}-cp{version}mu-manylinux2010_x86_64.whl".format(version=version),
            "cp{version}-cp{version}mu-manylinux1_x86_64.whl".format(version=version),
        ])
    else:
        suffixes.extend([
            "cp{version}-cp{version}m-manylinux_2_17_x86_64.manylinux2014_x86_64.whl".format(version=version),
            "cp{version}-cp{version}-manylinux_2_17_x86_64.manylinux2014_x86_64.whl".format(version=version),
            "cp{version}-cp{version}m-manylinux2010_x86_64.whl".format(version=version),
            "cp{version}-cp{version}-manylinux2010_x86_64.whl".format(version=version),
            "cp{version}-cp{version}m-manylinux1_x86_64.whl".format(version=version),
            "cp{version}-cp{version}-manylinux1_x86_64.whl".format(version=version),
            "cp{version}-abi3-manylinux2010_x86_64.whl".format(version=version),
            "cp{version}-abi3-manylinux1_x86_64.whl".format(version=version),
            "cp34-abi3-manylinux1_x86_64.whl",
            "cp34-abi3-manylinux2010_x86_64.whl",

        ])

    suffixes.extend([
        "py{major_version}-none-any.whl".format(major_version=version[:1]),
        "py2.py3-none-any.whl",
    ])

    return suffixes


def _install_cached_manylinux_version(cache_path, path, dependency, runtime):

    # get every known wheel out of the cache
    available_wheels = load_cached_wheels(cache_path)

    # determine the correct name for the wheel we want
    suffixes = _get_wheel_suffixes(runtime)

    wheel_name = None
    for suffix in suffixes:
        maybe_wheel_name = "{}-{}-{}".format(
            dependency.key,
            dependency.version,
            suffix,
        )

        # see if it's a match
        if maybe_wheel_name.lower() in available_wheels:
            wheel_name = maybe_wheel_name
            break

        # try replacing - with _ as well
        maybe_wheel_name = "{}-{}-{}".format(
            dependency.key.replace("-", "_"),
            dependency.version,
            suffix,
        )

        if maybe_wheel_name.lower() in available_wheels:
            wheel_name = maybe_wheel_name
            break

    if wheel_name is None:
        return False

    # unpack the cached wheel into our output
    wheel_path = available_wheels[wheel_name.lower()]

    with zipfile.ZipFile(wheel_path) as zf:
        zf.extractall(path)

    # success
    return True


def load_cached_wheels(path):

    ret = {}

    for root, _, files in os.walk(path):
        for file in files:
            if file.endswith(".whl"):
                ret[file] = os.path.join(root, file)
                ret[file.lower()] = os.path.join(root, file)

    return ret


def install_local_package(path, dependency):

    if os.path.isfile(dependency.location):
        if dependency.location.endswith(".egg"):
            return install_local_package_from_egg(path, dependency)
        else:
            raise Exception("Unable to install local package for {}"
                            .format(dependency))
    elif os.path.isdir(dependency.location):

        # see if it's just an egg file inside the directory
        egg_zip_path = os.path.join(
            dependency.location,
            dependency.egg_name() + ".egg",
        )
        if os.path.isfile(egg_zip_path):
            return install_local_package_from_egg(
                path,
                dependency,
                egg_path=egg_zip_path,
            )

        to_copy = []
        to_find = []
        shared_objects = []

        # XXX: never include these and assume the execution environment includes
        # them
        ignored_shared_objects = [
            "libc.so",
            "libz.so",
            "ld-linux-x86-64.so",
        ]

        top_level_path = _locate_top_level(dependency)
        if not top_level_path:

            # last ditch attempt, assume that the key of the package is the
            # actual folder name
            package_with_top_level = os.path.join(
                dependency.location,
                dependency.key,
            )

            if not os.path.exists(package_with_top_level):
                raise Exception("Unable to install local package for {}, "
                                "top_level.txt was not found"
                                .format(dependency))

            # well... we found something
            to_copy.append(dependency.key)

        # read folder names out of top_level.txt
        else:
            with open(top_level_path, "r") as fp:
                for line in fp:
                    line = line.strip()

                    if not line:
                        continue

                    # in a special case for pyyaml, skip the _yaml (which is
                    # for the .so)
                    if dependency.key == "pyyaml" and line == "_yaml":
                        continue

                    # similar case for cffi
                    if dependency.key == "cffi" and line == "_cffi_backend":
                        continue

                    if dependency.key == "cryptography":

                        module_path = pathlib.Path(dependency.location)

                        for found_path in module_path.rglob(line + ".*.so"):
                            elf_data = readelf(found_path.as_posix())

                            elf_dependencies = get_dependencies_from_elf_data(elf_data)

                            for elf_dependency in elf_dependencies:
                                is_ignored = is_ignored_shared_object(
                                    elf_dependency,
                                    ignored_shared_objects,
                                )
                                if is_ignored:
                                    continue

                                if elf_dependency not in shared_objects:
                                    shared_objects.append(elf_dependency)

                    # similar cases for cryptography
                    if dependency.key == "cryptography" and line in ("_openssl", "_padding", "_constant_time"):
                        continue

                    # similar case for pyrsistent's pvectorc
                    if dependency.key == "pyrsistent" and line == "pvectorc":
                        continue

                    # python packaging makes no sense, case in point: setuptools
                    if dependency.key == "setuptools" and line == "dist":
                        continue

                    # avoid a situation like:
                    # ['websockets', 'websockets/extensions', 'websockets/legacy']
                    if not _is_path_common_to_any(line, to_copy):
                        to_copy.append(line)

                    if dependency.key == "xmlsec" and line == "xmlsec":
                        to_find.append("xmlsec.*.so")
                        shared_objects.extend([
                            "libxmlsec1-openssl.so",
                            "libxmlsec1.so",
                            "libxmlsec1-openssl.so.1",
                            "libxmlsec1.so.1",
                            "libxml2.so",
                            "libcrypto.so",
                            "libxslt.so",
                            "libicuuc.so",
                            "libicudata.so",
                        ])

                    if dependency.key == "python-magic":
                        shared_objects.extend([
                            "libmagic.so.1",
                        ])

        # locate any findables
        for item in to_find:
            source = os.path.join(dependency.location, item)
            for found_path in glob.glob(source):
                found_filename = found_path[len(dependency.location) + 1:]
                to_copy.append(found_filename)

        # copy each found folder into our output
        for folder in to_copy:

            source = os.path.join(dependency.location, folder)
            destination = os.path.join(path, folder)

            # maybe we're dealing with a .py file instead of a directory
            if not os.path.exists(source):
                py_source = source + ".py"
                if os.path.exists(py_source):
                    source = py_source
                    destination = destination + ".py"

            if os.path.isfile(source):
                shutil.copyfile(source, destination)
            else:
                shutil.copytree(
                    source,
                    destination,
                    ignore=shutil.ignore_patterns(*IGNORED),
                )

        if shared_objects:

            ldconfig_process = subprocess.Popen(
                [
                    "ldconfig",
                    "-v",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

            ld_library_paths = []

            for line in ldconfig_process.stdout:

                line = line.decode("utf-8")

                if line.startswith("\t"):
                    continue

                ld_library_path = line.strip()
                ld_library_path = ld_library_path.strip(":")

                ld_library_paths.append(ld_library_path)

            ldconfig_process.communicate()

            shared_objects = find_shared_objects(
                shared_objects,
                ld_library_paths,
                ignored_dependencies=ignored_shared_objects,
            )

            for shared_object in shared_objects:

                logger.info("from {} including shared object {}".format(dependency.key, shared_object))

                if not ld_library_paths:
                    raise Exception(
                        "shared libraries required but ld_library_paths is empty"
                    )

                found_shared_object = False

                for ld_library_path in ld_library_paths:
                    maybe_library_path = os.path.join(
                        ld_library_path,
                        shared_object,
                    )

                    if os.path.exists(maybe_library_path):
                        output_path = os.path.join(path, shared_object)
                        shutil.copyfile(maybe_library_path, output_path,follow_symlinks=True)

                        found_shared_object = True
                        break

                if not found_shared_object:
                    raise Exception(
                        "unable to find shared object {}"
                        .format(shared_object)
                    )

    else:
        raise Exception("Unable to install local package for {}, neither a "
                        "file nor a directory".format(dependency))

    # success
    return True


def find_shared_objects(shared_objects, ld_library_paths, ignored_dependencies=None):

    ret = []

    for shared_object in shared_objects:

        ret.append(shared_object)

        dependencies = find_shared_object_dependencies(
            shared_object,
            ld_library_paths,
            ignored_dependencies=ignored_dependencies,
        )

        ret.extend(
            find_shared_objects(
                dependencies,
                ld_library_paths,
                ignored_dependencies=ignored_dependencies,
            )
        )

    return sorted(list(set(ret)))


def find_shared_object_dependencies(shared_object, ld_library_paths, ignored_dependencies=None):

    for ld_library_path in ld_library_paths:
        maybe_library_path = os.path.join(
            ld_library_path,
            shared_object,
        )

        if os.path.exists(maybe_library_path):
            elf_data = readelf(maybe_library_path)

            dependencies = get_dependencies_from_elf_data(elf_data)

            if ignored_dependencies is not None:
                filtered_dependencies = []
                for dependency in dependencies:

                    is_ignored = is_ignored_shared_object(
                        dependency,
                        ignored_dependencies,
                    )
                    if is_ignored:
                        continue

                    filtered_dependencies.append(dependency)

                dependencies = filtered_dependencies

            return dependencies

    return []


def is_ignored_shared_object(dependency, ignored_dependencies):
    if ignored_dependencies is None:
        return False

    for ignored_dependency in ignored_dependencies:
        if dependency.startswith(ignored_dependency):
            return True

    return False


def readelf(library_path):

    readelf_process = subprocess.Popen(
        [
            "readelf",
            "-d",
            library_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    elf_data = []

    for line in readelf_process.stdout:

        line = line.decode("utf-8")

        elf_line = line.strip()

        elf_data.append(elf_line)

    readelf_process.communicate()

    return elf_data


def get_dependencies_from_elf_data(elf_data):

    dependencies = []

    for line in elf_data:
        if line.startswith("0x0000000000000001"):

            _, _, value = parse_elf_dependency_line(line)

            if not value.startswith("Shared library:"):
                raise ValueError(
                    "unexpected value {!r} while processing elf depedency data "
                    "{!r}"
                    .format(value, line)
                )

            library = value[len("Shared library: "):]
            library = library.strip("[")
            library = library.strip("]")

            dependencies.append(library)

    return dependencies


def parse_elf_dependency_line(line):
    match = re.search(r'^(0x[0-9a-f]+)\s+[(](\w+)[)]\s+(.+$)$', line)

    if not match:
        return None, None, None

    return (
        match.groups()[0],
        match.groups()[1],
        match.groups()[2],
    )


def install_local_package_from_egg(path, dependency, egg_path=None):

    if egg_path is None:
        egg_path = dependency.location

    with zipfile.ZipFile(egg_path) as zf:
        data = zf.read("EGG-INFO/top_level.txt")
        data = data.decode("utf-8")

        to_copy = []
        for line in data.split("\n"):
            line = line.strip()

            if not line:
                continue

            to_copy.append(line)

        # determine which files to extract
        all_names = zf.namelist()

        for folder in to_copy:
            maybe_names_to_copy = []
            for name in all_names:
                if name.startswith(folder + "/"):
                    maybe_names_to_copy.append(name)
                elif name == folder + ".py":
                    maybe_names_to_copy.append(name)

            # filter our files to only keep what we want
            names_to_copy = []
            for name in maybe_names_to_copy:

                # filter out ignored
                skip = False
                for ignored in IGNORED:
                    if fnmatch.fnmatch(name, ignored):
                        skip = True
                        break

                if skip:
                    continue

                # append anything that isn't a .py file
                if not name.endswith(".py"):
                    names_to_copy.append(name)

                # and make sure we copy the .py file if there is no .pyc file
                pyc_name = name + "c"
                if pyc_name not in maybe_names_to_copy:
                    names_to_copy.append(name)

            # extract all the files to our output location
            zf.extractall(path, names_to_copy)

        # hopefully
        return True


def _locate_top_level(dependency):

    paths_to_try = []

    # unzipped egg?
    if dependency.location.endswith(".egg"):
        paths_to_try.append(os.path.join(dependency.location, "EGG-INFO"))

    # something else
    else:

        # could be a plain .egg-info folder, or a .egg/EGG-INFO setup
        egg_info_path = os.path.join(
            dependency.location,
            dependency.key + ".egg-info",
        )
        paths_to_try.append(egg_info_path)

        # could be a plain .egg-info folder on the egg name
        egg_info_path = os.path.join(
            dependency.location,
            dependency.egg_name() + ".egg-info",
        )
        paths_to_try.append(egg_info_path)

        # also try replacing - with _ for a local .egg-info
        egg_info_path = os.path.join(
            dependency.location,
            dependency.key.replace("-", "_") + ".egg-info",
        )
        paths_to_try.append(egg_info_path)

        egg_name = dependency.egg_name()
        egg_info_path = os.path.join(
            dependency.location,
            egg_name + ".egg",
            "EGG-INFO",
        )
        paths_to_try.append(egg_info_path)

        # could also be a .dist-info bundle
        dist_info_name = "{}-{}.dist-info".format(
            dependency.key.replace("-", "_"),
            dependency.version,
        )
        dist_info_path = os.path.join(dependency.location, dist_info_name)
        paths_to_try.append(dist_info_path)

        # and try capitalized name in dist info, too
        rr = dependency.as_requirement()
        dist_info_name = "{}-{}.dist-info".format(
            rr.name.replace("-", "_"),
            dependency.version,
        )
        dist_info_path = os.path.join(dependency.location, dist_info_name)
        paths_to_try.append(dist_info_path)

    # loop our paths
    for path in paths_to_try:

        # return the first existing top_level.txt found
        top_level_path = os.path.join(path, "top_level.txt")
        if os.path.isfile(top_level_path):
            return top_level_path

    # uh oh
    return None


def _is_path_common_to_any(path, parents):
    """Return true if path is the subpath of any path in parents."""

    is_child = False

    if len(parents) > 0:

        # use 'join' to force append a trailing '/'
        current_path_abs = os.path.join(os.path.realpath(path), "")

        for parent_path in parents:
            parent_path_abs = os.path.realpath(parent_path)

            if os.path.commonprefix([current_path_abs, parent_path_abs]) == parent_path_abs:
                is_child = True
                break

    return is_child


def install_project(path, name):

    logger.info("[{}] installing project".format(name))

    import pip._vendor.pkg_resources

    package = pip._vendor.pkg_resources.working_set.by_key[name]

    rv = install_local_package(path, package)

    logger.info("[{}] done installing project".format(name))

    return rv


def prune_python_files(path, prefer_pyc=True):

    # collect all .py files and __pycache__ dirs
    py_files = []
    pycache_dirs = []
    for root, dirs, files in os.walk(path):
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))

        for folder in dirs:
            if folder == "__pycache__":
                pycache_dirs.append(os.path.join(root, folder))

    # erase all pycache dirs
    for pycache_path in pycache_dirs:
        shutil.rmtree(pycache_path)

    # determine if they have a corresponding .pyc
    for py_file in py_files:

        pyc_file = py_file + "c"

        # if prefer_pyc is true, delete the corresponding .py file. otherwise,
        # delete the .pyc file
        if os.path.exists(pyc_file):
            if prefer_pyc:
                os.unlink(py_file)
            else:
                os.unlink(pyc_file)


def insert_shim(path, type, entry):

    if type == "plain":
        write_plain_shim(path, entry)
    elif type == "flask":
        install_flask_resources(path)
        write_flask_shim(path, entry)
    elif type in ("flask-eb", "flask-eb-reqs"):
        write_eb_shim(path, entry)


def write_plain_shim(path, entry):
    index_path = os.path.join(path, "index.py")
    with open(index_path, "w") as fp:
        fp.write(entry)


def install_flask_resources(path):

    # locate spindrift's wsgi file
    import spindrift
    init_path = spindrift.__file__
    lib_path, _ = os.path.split(init_path)
    wsgi_path = os.path.join(lib_path, "wsgi.py")

    # create a spindrift folder
    spindrift_output_path = os.path.join(path, "spindrift")
    if not os.path.exists(spindrift_output_path):
        os.makedirs(spindrift_output_path)

        # add an __init__.py
        spindrift_init_output_path = os.path.join(
            spindrift_output_path,
            "__init__.py",
        )
        with open(spindrift_init_output_path, "w"):
            pass

        # copy the wsgi.py file in
        spindrift_wsgi_output_path = os.path.join(
            spindrift_output_path,
            "wsgi.py",
        )
        shutil.copyfile(wsgi_path, spindrift_wsgi_output_path)


def write_flask_shim(path, entry):
    index_path = os.path.join(path, "index.py")
    with open(index_path, "w") as fp:
        fp.write("import spindrift.wsgi\n")
        fp.write(entry)
        #fp.write("import sys\n")
        #fp.write("import logging\n")
        #fp.write("ch = logging.StreamHandler(sys.stdout)\n")
        #fp.write("ch.setLevel(logging.DEBUG)\n")
        #fp.write("formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')\n")
        #fp.write("ch.setFormatter(formatter)\n")
        #fp.write("logging.getLogger().addHandler(ch)\n")
        fp.write("\n")
        fp.write("def handler(event, context):\n")
        fp.write("    return spindrift.wsgi.handler(app, event, context)\n")


def write_eb_shim(path, entry):
    index_path = os.path.join(path, "application.py")
    with open(index_path, "w") as fp:
        fp.write(entry)


def insert_requirements_txt(path, type, renamed_packages, installed_dependencies):
    import pip._internal.utils.misc

    if type != "flask-eb-reqs":
        return

    # determine which packages are local packages and exclude them. we assume
    # that packages installed with setup.py develop (which are editable) are
    # local packages to exclude from the requirements file.
    local_packages = pip._internal.utils.misc.get_installed_distributions(
        editables_only=True,
        include_editables=True,
    )

    requirements_txt_path = os.path.join(path, "requirements.txt")
    with open(requirements_txt_path, "w") as fp:

        for _, deps_in_section in installed_dependencies.items():

            for dep in deps_in_section:

                if dep in local_packages:
                    continue

                package_name = dep.key

                if renamed_packages is not None:

                    if isinstance(renamed_packages, dict):
                        if package_name in renamed_packages:
                            package_name = renamed_packages[package_name]
                    elif callable(renamed_packages):
                        package_name = renamed_packages(package_name)

                fp.write("{}=={}\n".format(package_name, dep.version))


def create_zip_bundle(path, zip_path):

    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(path):
            for file in files:

                # determine where in the zip file our real file ends up
                real_file_path = os.path.join(root, file)
                truncated = real_file_path[len(path):]
                truncated = truncated.lstrip(os.sep)

                # create a zip info object...
                zi = zipfile.ZipInfo(truncated)
                zi.compress_type = zipfile.ZIP_DEFLATED

                # ensure our files are readable
                # XXX: seems like a hack...
                zi.external_attr = 0o755 << 16

                # ...and put it in our zip file
                with open(real_file_path, "rb") as fp:
                    zf.writestr(zi, fp.read(), zipfile.ZIP_DEFLATED)


def output_zip_bundle(zip_path, destination):
    shutil.copyfile(zip_path, destination)
