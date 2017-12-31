# Copyright 2017, Ryan P. Kelly.

import argparse

import yaml

from spindrift.packager import package


class App(object):

    def run(self):

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--file",
            "-f",
            default="settings.spindrift",
            help="path to spindrift settings",
        )

        parser.add_argument(
            "command",
            choices=["package"],
            help="spindrift action to perform",
        )

        args = parser.parse_args()

        with open(args.file) as fp:
            settings = yaml.load(fp)

        if args.command == "package":
            package(
                settings["package"]["name"],
                settings["package"].get("type", "plain"),
                settings["package"]["entry"],
                settings["output"]["path"],
            )
        else:
            raise Exception("Implementation Error")
