import importlib.resources
import itertools
import logging
import ssl

import pytest

from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
import selenium.webdriver.support.expected_conditions as EC

import tornado.httpclient
import tornado.httpserver
import tornado.httputil
import tornado.ioloop
import tornado.iostream
import tornado.log
import tornado.tcpclient
import tornado.template
import tornado.testing
import tornado.web

TIMEOUT = 5

counter = itertools.count()


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
            """,
        }
    )

    class LoginHandler(base_handler):
        def get(self):
            logging.warning("in handler")
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




@pytest.fixture(params=[pytest.param(False, id="http"), pytest.param(True, id="https")])
def http_server(request, run_on_loop, proxy_map):
    protocol = "https" if request.param else "http"
    domain = f"tornadotest{next(counter)}.com"

    def create_server():
        nonlocal protocol, domain
        app = make_app(tornado.web.RequestHandler, tornado.web.Application)
        sock, port = tornado.testing.bind_unused_port()
        logging.warning("starting %s server on port %d", protocol, port)
        if protocol == "https":
            tt_path = importlib.resources.files("tornado.test")
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            with (
                importlib.resources.as_file(tt_path.joinpath("test.crt")) as cert,
                importlib.resources.as_file(tt_path.joinpath("test.key")) as key,
            ):
                ssl_ctx.load_cert_chain(cert, key)
            server = tornado.httpserver.HTTPServer(app, ssl_options=ssl_ctx)
            proxy_map.set(protocol, f"{domain}:443", f"127.0.0.1:{port}")
        else:
            server = tornado.httpserver.HTTPServer(app)
            proxy_map.set(protocol, domain, f"127.0.0.1:{port}")

        server.add_sockets([sock])
        return server

    server = run_on_loop(create_server)
    yield f"{protocol}://{domain}"
    logging.warning("stopping server")
    server.stop()


def test_simple(driver, http_server):
    wait = WebDriverWait(driver, TIMEOUT)
    driver.get(f"{http_server}/login")
    wait.until(EC.presence_of_element_located((By.ID, "submit")))
    button = driver.find_element(By.ID, "submit")
    button.submit()
    wait.until(EC.title_is("success"))
    print(driver.page_source)


