# Copyright 2017-2019, Ryan P. Kelly.

import argparse
import os.path

import yaml

from spindrift.packager import package


class App(object):

    def run(self):

        parser = argparse.ArgumentParser()

        parser.add_argument(
            "command",
            choices=["package"],
            help="spindrift action to perform",
        )

        parser.add_argument(
            "--file",
            "-f",
            help="path to spindrift settings",
        )

        parser.add_argument(
            "--package-name",
            help="name of the package you want to package",
        )

        parser.add_argument(
            "--package-type",
            help="what kind of package you are creating",
            choices=["plain", "flask", "flask-eb"],
        )

        parser.add_argument(
            "--package-entry",
            help=("entry point to your code. should either be a handler "
                  "function or the flask app, and it must be imported as "
                  "handler or as app for lambda, or application for elastic "
                  "beanstalk"),
        )

        parser.add_argument(
            "--package-runtime",
            help="the runtime to package for",
            choices=["python2.7", "python3.6"],
        )

        parser.add_argument(
            "--output-path",
            help="where to output the resulting zip file",
        )

        args = parser.parse_args()

        settings = {}

        # figure out how to open our settings file, if we're using one
        settings_path = None

        if os.path.exists("settings.spindrift"):
            settings_path = "settings.spindrift"

        if args.file:
            settings_path = args.file

        if settings_path is not None:
            with open(settings_path) as fp:
                settings.update(yaml.load(fp))

        other_arguments = {
            "package": {
                "name",
                "type",
                "entry",
                "runtime",
            },
            "output": {
                "path",
            },
        }

        for section, names in other_arguments.items():
            for name in names:
                arg_name = "{}_{}".format(section, name)
                arg_value = getattr(args, arg_name)

                if arg_value:
                    if section not in settings:
                        settings[section] = {}
                    settings[section][name] = arg_value

        if args.command == "package":
            package(
                settings["package"]["name"],
                settings["package"].get("type", "plain"),
                settings["package"]["entry"],
                settings["package"]["runtime"],
                settings["output"]["path"],
            )
        else:
            raise Exception("Implementation Error")
