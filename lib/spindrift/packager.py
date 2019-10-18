# Copyright 2017-2019, Ryan P. Kelly.

import fnmatch
import io
import logging
import os.path
import re
import shutil
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


def package(package, type, entry, runtime, destination, download=True, cache_path=None, renamed_packages=None, prefer_pyc=True, boto_handling="default"):
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

    """

    # determine what our dependencies are
    dependencies = find_dependencies(
        type,
        package,
        renamed_packages,
        boto_handling=boto_handling,
    )

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

            installed_dependencies.setdefault("install_manylinux_version", [])
            installed_dependencies["install_manylinux_version"].append(dependency)

            _mangle_package(path, dependency)
            continue

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

                installed_dependencies.setdefault("download_and_install_manylinux_version", [])
                installed_dependencies["download_and_install_manylinux_version"].append(dependency)

                _mangle_package(path, dependency)
                continue

        # if we get this far, use whatever package we have installed locally
        rv = install_local_package(path, dependency)
        if rv:

            logger.info(
                "[{}] installed {} via install_local_package"
                .format(package, dependency.key)
            )

            installed_dependencies.setdefault("install_local_package", [])
            installed_dependencies["install_local_package"].append(dependency)

            _mangle_package(path, dependency)
            continue

        raise Exception("Unable to find suitable source for {}=={}"
                        .format(dependency.key, dependency.version))

    logger.info("[{}] done installing dependencies".format(package))

    return installed_dependencies


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

    if runtime not in ("python2.7", "python3.6", "python3.7"):
        warnings.warn(
            "unknown runtime, packaging may fail (only 'python2.7', "
            "'python3.6', and 'python3.7' are known). attemping to parse "
            "version from runtime value {!r}"
            .format(runtime),
            Warning,
        )

    version = runtime.replace("python", "")
    version = version.replace(".", "")

    suffixes = [
        "py2.py3-none-any.whl",
    ]

    if runtime.startswith("python2."):
        suffixes.insert(
            0,
            "cp{version}-cp{version}mu-manylinux1_x86_64.whl".format(version=version),
        )
    else:
        suffixes.insert(
            0,
            "py{major_version}-none-any.whl".format(major_version=version[:1]),
        )

        if runtime.startswith("python3."):
            suffixes.insert(0, "cp34-abi3-manylinux1_x86_64.whl")

        suffixes.insert(
            0,
            "cp{version}-cp{version}m-manylinux1_x86_64.whl".format(version=version),
        )

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
            return install_local_package_from_egg(path, dependency)

        to_copy = []

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

                    to_copy.append(line)

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

    else:
        raise Exception("Unable to install local package for {}, neither a "
                        "file nor a directory".format(dependency))

    # success
    return True


def install_local_package_from_egg(path, dependency):

    with zipfile.ZipFile(dependency.location) as zf:
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

                # ensure our files are readable
                # XXX: seems like a hack...
                zi.external_attr = 0o755 << 16

                # ...and put it in our zip file
                with open(real_file_path, "rb") as fp:
                    zf.writestr(zi, fp.read(), zipfile.ZIP_DEFLATED)


def output_zip_bundle(zip_path, destination):
    shutil.copyfile(zip_path, destination)
