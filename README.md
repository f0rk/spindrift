spindrift
=========

`spindrift` is a library that helps package python applications for deployment
to AWS Lambda or AWS Elastic Beanstalk.

Currently, `spindrift` only supports "plain" and `flask` applications for
lambda and `flask` for elastic beanstalk, but support for additional deployment
modes are planned.

What `spindrift` does:
- packages your code and all necessary dependencies into a
  lambda compatible or elastic beanstalk compatible zip file.
- includes the appropriate shim so that your application doesn't need to
  know it's inside of lambda or elastic beanstalk.
- puts a zip package where you asked.
        
What `spindrift` doesn't:
- create any policies, S3 buckets, or other resources in AWS
- actually deploy anything to lambda or elastic beanstalk

If you're looking for a more out-of-the-box there's-only-this-one-way-to-do-it
approach for lambda, [zappa](https://github.com/Miserlou/Zappa) will be your
cup of tea.  It's a fantastic library that can definitely get you up and
running, albeit in the `zappa` way. If you're looking for a more modular
solution, `spindrift` is your drink of choice.

Usage
=====

`spindrift`'s configuration files are simple yaml affairs:
```!bash
~$ cat settings.spindrift
package:
  type: flask
  name: yourwebapp
  entry: from yourwebapp.main import app
  runtime: python3.6

output:
  path: /tmp/yourwebapp.zip
```

This file tells `spindrift` that it is working with a `flask` application and
where to find the `app`. It also tells `spindrift` to save the output to
`/tmp/yourwebapp.zip`.

You then run `spindrift` like so:
```!bash
~$ spindrift package
```

And your code should be packaged up at `/tmp/yourwebapp.zip`.

The configuration file and all configuration items are
optional, as everything can be specified via the command line:
```!bash
~$ spindrift package \
    --package-name yourwebapp \
    --package-type flask \
    --package-entry 'from yourwebapp.main import app' \
    --package-runtime python3.6 \
    --output-path /tmp/yourwebapp.zip
```

Now that a package has been created, you'll actually need to deploy it to
lambda. You can do this manually, or you can use a proper orchestration tool.

The usage for elastic beanstalk is slightly different:
```!bash
~$ spindrift package \
    --package-name yourwebapp \
    --package-type flask-eb \
    --package-entry 'from yourwebapp.main import app as application' \
    --package-runtime python3.6 \
    --output-path /tmp/yourwebapp.zip
```

How it Works
============

`spindrift` will automatically determine all dependencies and their versions
then copy all of the dependencies into a clean, new folder structure. Where
applicable, `spindrift` will use locally-available or architecture-specific
wheels to ensure that nothing needs to be compiled specifically to run on
lambda or elastic beanstalk.

Note that most other solutions package everything they can find in your
virtualenv. `spindrift` only tries to identify declared dependencies. Because
of this, you must make sure that your code is a package and that you've run
`python setup.py develop` with all of your dependencies. Internally,
`spindrift` is utilizing `pip` and information contained in each package's
`top_level.txt` to figure out what files to include.

Lambda and elastic beanstalk then expect a file in a certain importable format.
`spindrift` adds the appropriate file, and zips the whole package up. If an
output path is specified, `spindrift` will copy the package to appropriate
output path.

Library
=======

`spindrift` is also meant to be used as a library to package your code, so you
can perform other steps that are appropriate to your build system.

`spindrift`'s `packager` module contains many functions, but the most important
one is `package`. The values that `package` accepts should be familiar from the
command line usage above:
```!python
import spindrift.packager

spindrift.packager.package(
    "yourwebapp", # the name of your package
    "flask", # the type of package you want to create
    "from yourwebapp.app import app", # the entry point
    "python3.6", # the runtime
    "/tmp/yourwebapp.zip", # where to send the output
)
```

This makes `spindrift` easy to integrate into your existing tools as well as
facilitating the development of new tools yourself.

For example, `spindrift` can be used in conjunction with a `terraform` setup
that fetches your code from an S3 bucket. This involves a few steps:
- Package your code
- Upload code to S3
- Calculate base64sha256 of the package
- Upload that to S3 as well

You can definitely glue all of this together as separate commands, but it's
simpler to use `spindrift` as a library to accomplish what you need:
```!python
import base64
import hashlib
import tempfile

import botocore.session
import spindrift.packager


with tempfile.NamedTemporaryFile(suffix=".zip") as tf:
    spindrift.packager.package(
        "yourwebapp",
        "flask",
        "from yourwebapp.app import app",
        "python3.6",
        tf.name,
    )

    bs = botocore.session.get_session()
    s3_client = bs.create_client(service_name="s3")

    s3_client.put_object(
        Bucket="your-code-bucket",
        Key="yourwebapp.zip",
        Body=tf,
    )

    # now calculate the hash and store a key with that, so terraform can detect
    # the change
    tf.flush()
    tf.seek(0)

    m = hashlib.sha256()
    while True:
        chunk = tf.read(1024 * 1024)
        if not chunk:
            break
        m.update(chunk)

    encoded_hash = base64.b64encode(m.digest())

    s3_client.put_object(
        Bucket="your-code-bucket",
        Key="yourwebapp.zip.base64sha256",
        Body=encoded_hash,
        ContentType="text/plain",
    )
```
