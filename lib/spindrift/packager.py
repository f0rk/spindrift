# Copyright 2017-2018, Ryan P. Kelly.

import fnmatch
import os.path
import shutil
import tarfile
import tempfile
import zipfile

import requests
from lambda_packages import lambda_packages as _lambda_packages

import spindrift.compat


IGNORED = [
    "__pycache__",
    ".git",
    "__pycache__/*",
    ".git/*",
    "*/__pycache__/*",
    "*/.git/*",
]


lambda_packages = {k.lower(): v for k, v in _lambda_packages.items()}


def package(package, type, entry, runtime, destination):
    """Package up the given package.

    :param package: The name of the package to bundle up.
    :param type: The type of package to create. Currently, the only valid
        values are `"plain"`, which is for simple lambda functions written in
        the standard `handler(event, context)` style, and `"flask"` for flask
        applications. Use `"flask-eb"` for elastic beanstalk applications,
        which must be flask applications.
    :param entry: A string describing the entrypoint of the application. For
        type `"plain"` applications, this should import the handler function
        itself and name it `handler`, i.e. `from yourplainapp.handlers import
        snake_handler as handler`.  For `"flask"` applications, this should
        import your application and call it `app`, i.e., `from yourwebapp.app
        import api_app as app`. For `"flask-eb"` applications, this should
        import your application and call it `application`.
    :param runtime: The runtime to package for. Must be either `"python2.7"` or
        `"python3.6"`.
    :param destination: A path on the file system to store the resulting file.
        No parent directories will be created, so you must ensure they exist.

    """

    # determine what our dependencies are
    dependencies = find_dependencies(type, package)

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
        )

        # ...and create the archive
        output_archive(temp_path, destination)


def populate_directory(path, package, type, entry, runtime, dependencies):

    # install our dependencies
    install_dependencies(path, package, runtime, dependencies)

    # install our project itself
    install_project(path, package)

    # prune away any unused files
    prune_python_files(path)

    # insert our shim
    insert_shim(path, type, entry)


def output_archive(path, destination):

    # create a temporary file to zip into
    with tempfile.NamedTemporaryFile(suffix=".zip") as temp_file:

        # create our zip bundle
        create_zip_bundle(path, temp_file.name)

        # output our zip bundle to the given destination
        output_zip_bundle(temp_file.name, destination)


def find_dependencies(type, package_name):
    import pip

    package = pip._vendor.pkg_resources.working_set.by_key[package_name]

    # boto is available on lambda, don't repackage it
    # XXX: make this configurable?
    if package.key in ("boto3", "botocore"):
        if type != "flask-eb":
            return []

    ret = [package]

    requires = package.requires()
    for requirement in requires:
        ret.extend(find_dependencies(type, requirement.key))

    return list(set(ret))


def install_dependencies(path, package, runtime, dependencies):

    # for each dependency
    for dependency in dependencies:

        # don't try to install our own code this way, we'll never need to
        # download or want to override it
        if dependency.key == package:
            continue

        # each of the functions below will return false if they couldn't
        # perform the request operation, or true if they did. perform the
        # attempts in order, and skip the remaining options if we succeed.

        # determine if we have a matching precompiled-version available
        rv = install_matching_precompiled_version(path, dependency, runtime)
        if rv:
            continue

        # if not, see if we've got a manylinux version
        rv = install_manylinux_version(path, dependency, runtime)
        if rv:
            continue

        # maybe try downloading and installing a manylinux version?
        rv = download_and_install_manylinux_version(path, dependency, runtime)
        if rv:
            continue

        # still nothing? go for any precompiled-version
        rv = install_any_precompiled_version(path, dependency, runtime)
        if rv:
            continue

        # if we get this far, use whatever package we have installed locally
        rv = install_local_package(path, dependency)
        if not rv:
            raise Exception("Unable to find suitable source for {}=={}"
                            .format(dependency.key, dependency.version))


def install_matching_precompiled_version(path, dependency, runtime):
    return _install_precompiled_version(path, dependency, runtime, True)


def _install_precompiled_version(path, dependency, runtime, check_version):
    name = dependency.key

    # no matching package? False.
    if name not in lambda_packages:
        return False

    # no version for this runtime
    if runtime not in lambda_packages[name]:
        return False

    package = lambda_packages[name][runtime]

    # check for the correct version
    if check_version:
        if package["version"] != dependency.version:
            return False

    tf = tarfile.open(package["path"], mode="r:gz")
    for member in tf.getmembers():
        tf.extract(member, path=path)

    # yahtzee.
    return True


def install_manylinux_version(path, dependency, runtime):

    # XXX: where is this directory on other systems?
    wheel_cache_path = os.path.join(
        os.path.expanduser("~"),
        ".cache",
        "pip",
    )

    # sub out the rest of our work
    rv = _install_cached_manylinux_version(
        wheel_cache_path,
        path,
        dependency,
        runtime,
    )

    if not rv:
        fake_cache_path = _get_fake_cache_path()
        rv = _install_cached_manylinux_version(
            fake_cache_path,
            path,
            dependency,
            runtime,
        )

    return rv


def _get_fake_cache_path():
    return os.path.join(tempfile.gettempdir(), "spindrift_cache")


def download_and_install_manylinux_version(path, dependency, runtime):

    # pip's wheel cache is kinda an implementation detail, so just create our
    # cache dir for now
    fake_cache_path = _get_fake_cache_path()

    if not os.path.exists(fake_cache_path):
        os.makedirs(fake_cache_path)

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
    wheel_suffix = _get_wheel_suffix(runtime)
    if version not in data["releases"]:
        return False

    # and see if we can find the right wheel
    url = None
    for info in data["releases"][version]:
        if info["url"].endswith(wheel_suffix):
            url = info["url"]
            break

    # couldn't get the url, bail
    if url is None:
        return False

    # figure out what to save this url as
    wheel_name = "{}-{}-{}".format(name, version, wheel_suffix)
    wheel_path = os.path.join(fake_cache_path, wheel_name)

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


def _get_wheel_suffix(runtime):
    if runtime == "python2.7":
        suffix = "cp27mu-manylinux1_x86_64.whl"
    else:
        suffix = "cp36m-manylinux1_x86_64.whl"

    return suffix


def _install_cached_manylinux_version(cache_path, path, dependency, runtime):

    # no cache? punt
    if not os.path.isdir(cache_path):
        return False

    # get every known wheel out of the cache
    available_wheels = load_cached_wheels(cache_path)

    # determine the correct name for the wheel we want
    suffix = _get_wheel_suffix(runtime)

    wheel_name = "{}-{}-{}".format(
        dependency.key,
        dependency.version,
        suffix,
    )

    # see if it's a match
    if wheel_name not in available_wheels:
        return False

    # unpack the cached wheel into our output
    wheel_path = available_wheels[wheel_name]

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

    return ret


def install_any_precompiled_version(path, dependency, runtime):
    return _install_precompiled_version(path, dependency, runtime, False)


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

            # filter our files to only keey what we want
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
    import pip

    package = pip._vendor.pkg_resources.working_set.by_key[name]

    return install_local_package(path, package)


def prune_python_files(path):

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

        # and delete them if they do
        if os.path.exists(pyc_file):
            os.unlink(py_file)


def insert_shim(path, type, entry):

    if type == "plain":
        write_plain_shim(path, entry)
    elif type == "flask":
        install_flask_resources(path)
        write_flask_shim(path, entry)
    elif type == "flask-eb":
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


def create_zip_bundle(path, zip_path):

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
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
