spindrift
=========

`spindrift` is a library that helps package and deploy python applications to
AWS Lambda.

Currently, `spindrift` only supports "plain" and `flask` applications, but
support for additional deployment modes is planned.

What `spindrift` does:
- packages your code and all necessary dependencies into a
  lambda-compatible zip file.

- includes the appropriate shim so that your application doesn't need to
  know it's inside of lambda.
- helps you integrate with an infrastructure tool for deployment:
    - provides you with a `terraform` template to get you started.
    - planned support for a `cloudformation` template as well.
- puts a zip package where you asked.
        
What `spindrift` doesn't:
- create any policies, S3 buckets, or other resources in AWS
- actually deploy anything to lambda

If you're looking for a more out-of-the-box there's-only-this-one-way-to-do-it
approach, `zappa` will be your cup of tea. It's a fantastic library that can
definitely get you up and running, albeit in the `zappa` way. If you're looking
for a more modular solution, `spindrift` is your drink of choice.

Usage
=====

`spindrift`'s configuration files are simple .yaml affairs:
```!bash
~$ cat spindrift.yaml
package:
  type: flask
  name: yourwebapp
  entry: from yourwebapp.main import app

output:
  path: /tmp/yourwebapp.zip
```

This file tells `spindrift` that it is working with a `flask` application and
where to find the `app`. It also tells `spindrift` to save the output to
/tmp/yourwebapp.zip.  The output section is optional, as you can configure (or
override) the output destination on the command line.

To get `spindrift` to make a package for you:
```!bash
~$ spindrift package
```

`spindrift` will automatically determine all dependencies and their versions
then copy all of the dependencies into a clean, new folder structure. Where
applicable, `spindrift` will use locally-available or architecture-specific
wheels to ensure that nothing needs to be compiled specifically to run on
lambda.

Note that most other solutions package every old thing they can find in your
virtualenv. `spindrift` only tries to identify declared dependencies.

Lambda then expects a function that it can import and call. `spindrift` adds
the appropriate file, and zips the whole package up. If an output path is
specified, `spindrift` will copy the package to appropriate output path.

Now that a package has been created, you'll actually need to deploy it to
lambda. You can do this manually, or you can use a proper orchestration tool.
`spindrift` can generate templates for you for use with `terraform` like so:
```!bash
~$ spindrift terraform
```
