# Copyright 2017-2019, Ryan P. Kelly.

"""
Lambda-Flask WSGI shim. Only used by Lambda.
"""

import io
import sys
import urllib

from werkzeug.wrappers import Response
from werkzeug.wsgi import ClosingIterator


def handler(app, event, context):
    environ = create_wsgi_environ(event)

    # override some lambda specifics
    environ["HTTPS"] = "on"
    environ["wsgi.url_scheme"] = "https"
    environ["lambda.context"] = context

    # create a response
    response = Response.from_app(app, environ)

    # create the object we're going to send back to api gateway
    ret = {}

    # populate the body
    ret["body"] = response.get_data(as_text=True) # XXX: binary support...
    ret["isBase64Encoded"] = False

    # add in a status code
    ret["statusCode"] = response.status_code

    # add in headers
    ret["headers"] = {}
    for header, value in response.headers:
        ret["headers"][header] = value

    # boom.
    return ret


def create_wsgi_environ(event):

    # see https://www.python.org/dev/peps/pep-0333/

    # determine GET, POST, etc.
    method = event["httpMethod"]

    # determine the script name
    script_name = "" # XXX: this shouldn't always be root

    # decode the path being request
    path = event["path"]
    path = urllib.parse.unquote_plus(path)

    # format the query string
    query = event["queryStringParameters"]
    query_string = ""
    if query:
        query_string = urllib.parse.urlencode(query)

    # server name should be configurable?
    server_name = "spindrift"

    # fixup headers
    headers = event["headers"] or {}
    for header in headers:
        canonical = header.title()
        if header != canonical:
            headers[canonical] = headers.pop(header)

    # XXX: do we trust this?
    server_port = headers.get("X-Forwarded-Port", "80")

    # determine the remote address
    x_forwarded_for = headers.get("X-Forwarded-For", "")
    remote_addr = "127.0.0.1"
    if "," in x_forwarded_for:
        remotes = x_forwarded_for.split(",")
        remotes = [r.strip() for r in remotes]

        # last address is the load balancer, second from last is the actual
        # address
        if len(remotes) >= 2:
            remote_addr = remotes[-2]

    # XXX: do we trust this? isn't it always https?
    wsgi_url_scheme = headers.get("X-Forwarded-Proto", "http"),

    # retrieve the body and encode it
    body = event["body"]
    if isinstance(body, str):
        body = body.encode("utf-8")

    # setup initial environ dict
    environ = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": script_name,
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "SERVER_NAME": server_name,
        "SERVER_PORT": server_port,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": remote_addr,
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": wsgi_url_scheme,
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr, # XXX: this should be a logger.
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }

    # get content-type from headers
    content_type = headers.get("Content-Type")
    if content_type is not None:
        environ["CONTENT_TYPE"] = content_type

    # determine content-length from the body of request
    environ["CONTENT_LENGTH"] = 0
    if body:
        environ["CONTENT_LENGTH"] = len(body)

    # apply all HTTP_* headers into the environ
    for header, value in headers.items():
        key_name = header.replace("-", "_")
        key_name = key_name.upper()
        key_name = "HTTP_" + key_name
        environ[key_name] = value

    # send back our completed environ
    return environ


class SpindriftMiddleware(object):

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        return ClosingIterator(self.application(environ, start_response))
