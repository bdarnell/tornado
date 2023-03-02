import asyncio
import concurrent.futures
import contextlib
import logging
import os
import threading
import typing
import unittest
import urllib

from browserstack.local import Local
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
import selenium.webdriver.support.expected_conditions as EC

import tornado.httpclient
import tornado.httpserver
import tornado.httputil
import tornado.log
import tornado.template
import tornado.testing
import tornado.web

TIMEOUT = 5

# Copied from cpython 3.11 source. Remove this after upgrading.
def _enter_context(cm, addcleanup):
    # We look up the special methods on the type to match the with
    # statement.
    cls = type(cm)
    try:
        enter = cls.__enter__
        exit = cls.__exit__
    except AttributeError:
        raise TypeError(
            f"'{cls.__module__}.{cls.__qualname__}' object does "
            f"not support the context manager protocol"
        ) from None
    result = enter(cm)
    addcleanup(exit, cm, None, None, None)
    return result


def enterModuleContext(cm):
    """Same as enterContext, but module-wide."""
    return _enter_context(cm, unittest.addModuleCleanup)


@contextlib.contextmanager
def run_asyncio_thread():
    loop_future = concurrent.futures.Future()

    async def loop_main():
        nonlocal shutdown_event
        shutdown_event = asyncio.Event()
        loop_future.set_result((asyncio.get_running_loop(), shutdown_event))
        await shutdown_event.wait()

    def run_loop():
        loop = asyncio.new_event_loop()
        loop.run_until_complete(loop_main())

    thread = threading.Thread(target=run_loop)
    thread.start()
    loop, shutdown_event = loop_future.result()
    try:
        yield loop
    finally:
        run_on_loop(loop, shutdown_event.set)
        thread.join()


def run_on_loop(loop, fn, *args, **kwargs):
    fut = concurrent.futures.Future()

    def wrapped():
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as e:
            fut.set_exception(e)

    loop.call_soon_threadsafe(wrapped)
    return fut.result()

class ProxyDelegate(tornado.httputil.HTTPMessageDelegate):
    def __init__(self, server_conn, request_conn):
        self.server_conn = server_conn
        self.request_conn = request_conn
        
    def headers_received(self, start_line, headers):
        print('proxy received', start_line)
        self.headers = headers
        self.chunks = []

        self.method = start_line.method
        self.target = start_line.path

    def data_received(self, chunk):
        self.chunks.append(self)

    def finish(self):
        tornado.ioloop.IOLoop.current().spawn_callback(self.send_proxied_request)

    async def send_proxied_request(self):
        if self.method == "CONNECT":
            # detach and start forwarding to tcpclient
            pass
        else:
            # self.target is an absolute url. 
            parsed = urllib.parse.urlparse(self.target)
            parsed.netloc = proxy_map.get(parsed.scheme, parsed.netloc)

            http = tornado.httpclient.AsyncHTTPClient()
            resp = await http.fetch(parsed.geturl())
            await self.request_conn.write_headers(
                tornado.httputil.ResponseStartLine(
                    resp.code,
                    resp.reason,
                    "HTTP/1.1"
                ),
                resp.headers,
            )
            await self.request_conn.write(resp.body)
            self.request_conn.finish()

    def on_connection_close(self):
        pass


class Proxy(tornado.httputil.HTTPServerConnectionDelegate):
    def start_request(self, server_conn, request_conn):
        return ProxyDelegate(server_conn, request_conn)
    
    def on_close(self, server_conn):
        pass

class ProxyMap(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.map = {}

    def get(self, protocol, host):
        with self.lock:
            return self.map[(protocol, host)]

    def set(self, protocol, host, netloc):
        with self.lock:
            self.map[(protocol, host)] = netloc

proxy_map = ProxyMap()

def setUpModule():
    global proxy_asyncio_loop
    proxy_asyncio_loop = enterModuleContext(run_asyncio_thread())

    def start_server():
        server = tornado.httpserver.HTTPServer(Proxy())
        sock, port = tornado.testing.bind_unused_port()
        server.add_sockets([sock])
        return server, port
    global proxy_server
    proxy_server, proxy_port = run_on_loop(proxy_asyncio_loop, start_server)


    username = os.environ["BROWSERSTACK_USERNAME"]
    access_key = os.environ["BROWSERSTACK_ACCESS_KEY"]

    enterModuleContext(
        Local(
            key=access_key,
            force="true",  # kill existing process
            #forcelocal="true",  # Run everything through local. Doesn't seem to work with safari.
            forceProxy="true",
            localProxyHost="127.0.0.1", localProxyPort=f"{proxy_port}"
        )
    )

    global BS_URL
    BS_URL = f"https://{username}:{access_key}@hub.browserstack.com/wd/hub"


def tearDownModule():
    run_on_loop(proxy_asyncio_loop, proxy_server.stop)


def make_app(base_handler, app_factory):
    loader = tornado.template.DictLoader(
        {
            "form.html": """
                <form method="POST" action="/submit">
                {{xsrf_form_html()}}
                <input type="submit" id="submit"/>
                </form>
            """,
            "success.html": """
                <title>success</title>
                <body>
                success from {{request.protocol}}://{{request.host}}
                </body>
            """
        }
    )

    class LoginHandler(base_handler):
        def get(self):
            self.set_cookie("sesssion_id", "1234")
            self.redirect("/form")

    class FormHandler(base_handler):
        def get(self):
            self.render("form.html")

    class SubmitHandler(base_handler):
        def post(self):
            self.render("success.html")

    app = app_factory(
        [
            ("/login", LoginHandler),
            ("/form", FormHandler),
            ("/submit", SubmitHandler),
        ],
        template_loader=loader,
    )
    return app


class BaseBrowserTestCase(unittest.TestCase):
    def enterContext(self, cm):
        """Enters the supplied context manager.
        If successful, also adds its __exit__ method as a cleanup
        function and returns the result of the __enter__ method.
        """
        return _enter_context(cm, self.addCleanup)

    @classmethod
    def enterClassContext(cls, cm):
        """Same as enterContext, but class-wide."""
        return _enter_context(cm, cls.addClassCleanup)

    @classmethod
    def browser_options(cls):
        raise NotImplementedError()

    @classmethod
    def setUpClass(cls):
        options = cls.browser_options()
        options.set_capability(
            "bstack:options",
            {
                "local": "true",
                "networkLogs": "true",
                "networkLogsOptions": {"captureContent": "true"},
            },
        )
        cls.driver = cls.enterClassContext(
            webdriver.Remote(
                command_executor=BS_URL,
                options=options,
            )
        )

    def setUp(self):
        loop = self.enterContext(run_asyncio_thread())

        def start_server():
            app = make_app(tornado.web.RequestHandler, tornado.web.Application)
            server = tornado.httpserver.HTTPServer(app)
            sock, port = tornado.testing.bind_unused_port()
            server.add_sockets([sock])
            return server, port

        self.server, self.port = run_on_loop(loop, start_server)

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def get_url(self, path):
        return f"http://example532532.com:{self.port}{path}"

    def test_simple(self):
        self.driver.get(self.get_url("/login"))
        button = self.driver.find_element(By.ID, "submit")
        button.submit()
        WebDriverWait(self.driver, TIMEOUT).until(EC.title_is("success"))
        print(self.driver.page_source)


class FirefoxTestCase(BaseBrowserTestCase):
    @classmethod
    def browser_options(cls):
        from selenium.webdriver.firefox.options import Options

        return Options()


class ChromeTestCase(BaseBrowserTestCase):
    @classmethod
    def browser_options(cls):
        from selenium.webdriver.chrome.options import Options

        return Options()


class SafariTestCase(BaseBrowserTestCase):
    @classmethod
    def browser_options(cls):
        from selenium.webdriver.safari.options import Options

        options = Options()
        # Safari defaults to some ancient version if you don't
        # specify. 
        options.browser_version = "16"
        return options


del BaseBrowserTestCase

# del FirefoxTestCase
del ChromeTestCase
del SafariTestCase

if __name__ == "__main__":
    tornado.log.enable_pretty_logging()
    unittest.main(verbosity=2)
