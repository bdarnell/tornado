import functools
import importlib.resources
import itertools
import logging
import ssl
import typing

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

TIMEOUT = 10

counter = itertools.count()


class BaseHandler(tornado.web.RequestHandler):
    def session_cookie_name(self):
        return "sessionid"
    
class XSRFHostBaseHandler(BaseHandler):
    def set_cookie(self, name, *args, **kwargs):
        if name == "_xsrf":
            name = "__Host-xsrf"
        super().set_cookie(name, *args, secure=True, path="/", **kwargs)

    def get_cookie(self, name, *args, **kwargs):
        if name == "_xsrf":
            name = "__Host-xsrf"
        return super().get_cookie(name, *args, **kwargs)


@pytest.fixture(params=["none", "doublesubmit", "doublesubmithost"])
def good_app(request, protocol):
    xsrf_strategy = request.param
    base_handler: typing.Any = BaseHandler
    app_factory = tornado.web.Application
    match xsrf_strategy:
        case "none":
            pass
        case "doublesubmit":
            app_factory = functools.partial(tornado.web.Application, xsrf_cookies=True)
        case "doublesubmithost":
            if protocol == "http":
                pytest.xfail("host cookies rejected without https")
            base_handler = XSRFHostBaseHandler
            app_factory = functools.partial(tornado.web.Application, xsrf_cookies=True)
        case _:
            raise NotImplementedError()

    loader = tornado.template.DictLoader(
        {
            "form.html": """
                <form method="POST" action="/submit">
                {% module xsrf_form_html() %}
                <input type="submit" id="submit"/>
                </form>
            """,
            "success.html": """
                <span id="message">success from {{request.protocol}}://{{request.host}}</span>
            """,
            "failure.html": """
                <span id="message">error: {{message}}</span>
            """,
        }
    )

    class LoginHandler(base_handler):
        def get(self):
            self.set_cookie(self.session_cookie_name(), "1234")
            self.redirect("/form")

    class FormHandler(base_handler):
        def get(self):
            self.render("form.html")

    class SubmitHandler(base_handler):
        def post(self):
            if self.get_cookie(self.session_cookie_name()) != "1234":
                self.set_status(401)
                self.render("failure.html", message="no session cookie")
                return
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


@pytest.fixture(params=["http", "https"])
def protocol(request):
    return request.param


@pytest.fixture(params=["localhost", "nonlocal"])
def good_domain(request):
    if request.param == "localhost":
        return "localhost"
    return f"www.tornadotest{next(counter)}.com"


@pytest.fixture(autouse=True)
def prune_matrix(browser_options, good_domain):
    # Seems like there should be a better way to cut these combinations out
    # of the matrix. Maybe pytest_generate_tests?
    if browser_options.KEY == "safari.options" and good_domain == "localhost":
        # Safari has limits on the use of localhost. Browserstack works around
        # this by rewriting localhost to bs-local.com. This breaks our virtual
        # hosting but more importantly means the localhost tests aren't meaningful
        # (We're interested in things like "does the browser consider localhost
        # to be a secure context and allow __Host cookie prefixes", which isn't
        # answered by the rewritten host).
        pytest.skip("can't test localhost on safari")


@pytest.fixture
def good_base_url(run_on_loop, good_domain, good_app, protocol, proxy_map):
    domain = good_domain
    server, port = run_on_loop(start_server, good_app, protocol)

    if protocol == "https":
        proxy_map.set(protocol, f"{domain}:443", f"127.0.0.1:{port}")
    else:
        proxy_map.set(protocol, domain, f"127.0.0.1:{port}")

    yield f"{protocol}://{domain}"
    run_on_loop(server.stop)


@pytest.fixture(params=["unrelated", "subdomain", "sibling"])
def evil_domain(request, good_domain):
    match (request.param, good_domain):
        case "unrelated", _:
            return f"www.eviltest{next(counter)}.com"
        case _, "localhost":
            pytest.skip("no related domain tests for localhost")
        case "subdomain", good_domain:
            return f"evil.{good_domain}"
        case "sibling", good_domain if good_domain.startswith("www."):
            return f"evil.{good_domain.removeprefix('www.')}"
        case _, _:
            raise NotImplementedError()


## For manual testing of domain generation: This test will fail and print
## out all the domains used.
# def test_domains(good_domain, evil_domain):
#     assert good_domain == evil_domain


def start_server(app, protocol):
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
    else:
        server = tornado.httpserver.HTTPServer(app)

    server.add_sockets([sock])
    return server, port


def test_allow(driver, good_base_url):
    """Form submissions within a legitimate same-host session must be
    allowed without triggering XSRF protections.
    """
    wait = WebDriverWait(driver, TIMEOUT)
    driver.get(f"{good_base_url}/login")
    wait.until(EC.presence_of_element_located((By.ID, "submit")))
    button = driver.find_element(By.ID, "submit")
    button.submit()
    wait.until(EC.presence_of_element_located((By.ID, "message")))
    msg = driver.find_element(By.ID, "message")
    assert f"success from {good_base_url}" in msg.text
