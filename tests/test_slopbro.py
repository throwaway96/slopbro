import io
import os
import ssl
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import slopbro


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.runners = {}
        for name in ("run", "run_test_connection", "run_test_payload"):
            patcher = patch.object(slopbro, name)
            self.runners[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_normal_defaults(self):
        self.assertEqual(slopbro.main(["tv.local"]), 0)
        self.runners["run"].assert_called_once_with(
            "tv.local", debug=False, asset_source="auto",
            local_ip_override=None, webos_version_override=None,
            curl_insecure=False, fake_service_path=slopbro.DEFAULT_FAKE_SERVICE_PATH,
        )
        self.runners["run_test_connection"].assert_not_called()
        self.runners["run_test_payload"].assert_not_called()

    def test_option_value_forms(self):
        for equals in (False, True):
            argv = ["--debug", "tv.local", "--curl-insecure"]
            for option, value in (
                ("--local-ip", "192.168.1.2"),
                ("--asset-source", "dir"),
                ("--webos-version", "6.5"),
            ):
                argv.extend([option + "=" + value] if equals else [option, value])
            self.assertEqual(slopbro.main(argv), 0)
            self.runners["run"].assert_called_with(
                "tv.local", debug=True, asset_source="dir",
                local_ip_override="192.168.1.2", webos_version_override="6.5",
                curl_insecure=True, fake_service_path=slopbro.DEFAULT_FAKE_SERVICE_PATH,
            )

    def test_fake_service_path_options(self):
        for argv, expected in (
            (["--fake-service-path", "/custom/service"], "/custom/service"),
            (["--fake-service-path=/custom/service"], "/custom/service"),
            (["--no-fake-service-path"], ""),
            (["--fake-service-path=/custom/service", "--no-fake-service-path"], ""),
            (["--no-fake-service-path", "--fake-service-path=/custom/service"], "/custom/service"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(slopbro.main(argv + ["tv.local"]), 0)
                self.assertEqual(self.runners["run"].call_args.kwargs["fake_service_path"], expected)

        self.assertEqual(slopbro.main([
            "--test-server=payload", "--no-fake-service-path",
        ]), 0)
        self.assertEqual(self.runners["run_test_payload"].call_args.kwargs["fake_service_path"], "")

    def test_test_server_modes(self):
        for host in (None, "tv.local"):
            positional = [host] if host else []
            self.assertEqual(slopbro.main(["--test-server", "simple"] + positional), 0)
            self.runners["run_test_connection"].assert_called_with(
                host, local_ip_override=None,
            )
            self.assertEqual(slopbro.main([
                "--test-server=payload", "--debug", "--curl-insecure",
                "--asset-source=embedded", "--local-ip=192.168.1.2",
            ] + positional), 0)
            self.runners["run_test_payload"].assert_called_with(
                host, debug=True, asset_source="embedded",
                local_ip_override="192.168.1.2", curl_insecure=True,
                fake_service_path=slopbro.DEFAULT_FAKE_SERVICE_PATH,
            )
        self.runners["run"].assert_not_called()

    def test_help_aliases(self):
        for option in ("--help", "-h", "-?"):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                with self.assertRaises(SystemExit) as caught:
                    slopbro.main([option])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("--test-server", output.getvalue())
            self.assertIn("--no-fake-service-path", output.getvalue())
        for runner in self.runners.values():
            runner.assert_not_called()

    def test_invalid_arguments(self):
        invalid = [
            [], ["tv", "extra"], ["--unknown"],
            ["--asset-source=bad", "tv"], ["--test-server=bad"],
            ["--local-ip=bad", "tv"], ["--local-ip=", "tv"],
            ["--webos-version=", "tv"], ["--webos-version", " ", "tv"],
            ["--fake-service-path=", "tv"],
            ["--fake-service-path", " ", "tv"],
        ]
        invalid.extend([[option] for option in (
            "--local-ip", "--webos-version", "--asset-source", "--test-server",
            "--fake-service-path",
        )])
        for argv in invalid:
            with patch("sys.stderr", new_callable=io.StringIO) as output:
                with self.assertRaises(SystemExit) as caught:
                    slopbro.main(argv)
            self.assertEqual(caught.exception.code, 2, argv)
            self.assertIn("error:", output.getvalue())
        for runner in self.runners.values():
            runner.assert_not_called()

    def test_default_argv(self):
        with patch("sys.argv", ["slopbro.py", "tv.local"]):
            self.assertEqual(slopbro.main(), 0)
        self.runners["run"].assert_called_once()


class HandlerTests(unittest.TestCase):
    def _make_tracking_handler(
        self,
        path,
        tracker=None,
        tracked_files=("index.html",),
        root_dir=None,
        allow_embedded_assets=True,
        allow_filesystem_fallback=False,
    ):
        tracker = tracker or Mock()
        handler_class = slopbro.make_tracking_handler(
            root_dir,
            tracker,
            tracked_files,
            allow_embedded_assets,
            allow_filesystem_fallback,
        )
        handler = object.__new__(handler_class)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.statuses = []
        handler.headers = []
        handler.errors = []
        handler.send_response = lambda status, *args: handler.statuses.append(status)
        handler.send_header = lambda name, value: handler.headers.append((name, value))
        handler.end_headers = lambda: None
        handler.send_error = lambda status, message=None, *args: (
            handler.errors.append((status, message))
        )
        return handler, tracker

    def test_server_startup(self):
        for mode in ("payload", "simple"):
            for failed_attempts in (0, 1, 2):
                with self.subTest(mode=mode, failed_attempts=failed_attempts):
                    server = Mock()
                    outcomes = [OSError("port unavailable")] * failed_attempts
                    if failed_attempts < 2:
                        outcomes.append(server)
                    with patch.object(slopbro, "HTTPServer", side_effect=outcomes) as factory:
                        with patch.object(slopbro.threading, "Thread") as thread:
                            if mode == "simple":
                                starter = slopbro.start_test_connection_server
                                args = ()
                            else:
                                starter = slopbro.start_http_server
                                args = (None, None, [], True, False)
                            if failed_attempts == 2:
                                with self.assertRaisesRegex(RuntimeError, "could not start"):
                                    starter(*args, bind_host="127.0.0.1", preferred_port=9000)
                                thread.assert_not_called()
                            else:
                                self.assertIs(
                                    starter(*args, bind_host="127.0.0.1", preferred_port=9000),
                                    server,
                                )
                                thread.assert_called_once_with(
                                    target=server.serve_forever, daemon=True,
                                )
                                thread.return_value.start.assert_called_once_with()
                    handler = factory.call_args_list[0][0][1]
                    ports = [9000] if failed_attempts == 0 else [9000, 0]
                    self.assertEqual(factory.call_args_list, [
                        call(("127.0.0.1", port), handler) for port in ports
                    ])

    def test_get_marks_only_after_source_is_closed(self):
        source = io.BytesIO(b"payload")
        handler, tracker = self._make_tracking_handler("/index.html")
        handler._send_bytes = lambda rel_path, data: source
        tracker.mark_seen.side_effect = lambda rel_path: (
            self.assertTrue(source.closed),
            (True, 1, 1),
        )[1]

        with patch.object(slopbro, "_read_embedded_file", return_value=b"payload"):
            with patch.object(slopbro, "log") as logger:
                handler.do_GET()

        self.assertEqual(handler.wfile.getvalue(), b"payload")
        tracker.mark_seen.assert_called_once_with("index.html")
        logger.assert_called_once()

    def test_get_failure_closes_source_and_can_be_retried(self):
        tracker = slopbro.RequestedFilesTracker(["index.html"])
        handler, _ = self._make_tracking_handler("/index.html", tracker=tracker)
        sources = []
        original_send_bytes = handler._send_bytes

        def send_bytes(rel_path, data):
            source = original_send_bytes(rel_path, data)
            sources.append(source)
            return source

        handler._send_bytes = send_bytes

        def interrupted_copy(source, destination):
            raise OSError("client disconnected")

        handler.copyfile = interrupted_copy
        with patch.object(slopbro, "_read_embedded_file", return_value=b"payload"):
            with self.assertRaisesRegex(OSError, "client disconnected"):
                handler.do_GET()
            self.assertTrue(sources[0].closed)
            self.assertEqual(tracker.missing_files(), ["index.html"])
            handler.copyfile = lambda source, destination: destination.write(b"payload")
            with patch.object(slopbro, "log") as logger:
                handler.do_GET()
                handler.do_GET()

            self.assertEqual(handler.wfile.getvalue(), b"payloadpayload")
            self.assertTrue(all(source.closed for source in sources))
            self.assertEqual(tracker.missing_files(), [])
            self.assertEqual(logger.call_count, 1)

    def test_get_close_failure_does_not_mark_file(self):
        class CloseFailingBytesIO(io.BytesIO):
            def close(self):
                super().close()
                raise OSError("close failed")

        source = CloseFailingBytesIO(b"payload")
        tracker = slopbro.RequestedFilesTracker(["index.html"])
        handler, _ = self._make_tracking_handler("/index.html", tracker=tracker)
        handler._send_bytes = lambda rel_path, data: source

        with patch.object(slopbro, "_read_embedded_file", return_value=b"payload"):
            with self.assertRaisesRegex(OSError, "close failed"):
                handler.do_GET()

        self.assertTrue(source.closed)
        self.assertEqual(tracker.missing_files(), ["index.html"])

    def test_head_uses_inherited_handler_without_tracking(self):
        self.assertNotIn(
            "do_HEAD",
            slopbro.make_tracking_handler(None, Mock(), ["index.html"], True, False).__dict__,
        )
        get_tracker = slopbro.RequestedFilesTracker(["index.html"])
        get_handler, _ = self._make_tracking_handler(
            "/index.html", tracker=get_tracker,
        )
        tracker = slopbro.RequestedFilesTracker(["index.html"])
        head_handler, _ = self._make_tracking_handler(
            "/index.html", tracker=tracker,
        )
        with patch.object(slopbro, "_read_embedded_file", return_value=b"payload"):
            get_handler.do_GET()
            head_handler.do_HEAD()

        self.assertEqual(head_handler.statuses, get_handler.statuses)
        self.assertEqual(head_handler.headers, get_handler.headers)
        self.assertEqual(head_handler.wfile.getvalue(), b"")
        self.assertEqual(get_tracker.missing_files(), [])
        self.assertEqual(tracker.missing_files(), ["index.html"])

    def test_send_head_does_not_mark_file(self):
        handler, tracker = self._make_tracking_handler("/index.html")
        with patch.object(slopbro, "_read_embedded_file", return_value=b"payload"):
            source = handler.send_head()

        self.assertEqual(source.read(), b"payload")
        self.assertEqual(handler.statuses, [200])
        self.assertEqual(dict(handler.headers)["Content-Length"], "7")
        tracker.mark_seen.assert_not_called()
        source.close()

    def test_routes_preserve_sources_allowlist_and_missing_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "index.html"), "wb") as asset:
                asset.write(b"directory")
            with open(os.path.join(directory, "other.txt"), "wb") as asset:
                asset.write(b"untracked")
            embedded = {"index.html": b"embedded"}

            with patch.object(
                slopbro, "_read_embedded_file", side_effect=embedded.get,
            ) as read_embedded:
                directory_handler, _ = self._make_tracking_handler(
                    "/index.html?cache=1",
                    root_dir=directory,
                    allow_embedded_assets=False,
                    allow_filesystem_fallback=True,
                )
                self.assertEqual(directory_handler.send_head().read(), b"directory")
                read_embedded.assert_not_called()

                embedded_handler, _ = self._make_tracking_handler(
                    "/",
                    root_dir=directory,
                    allow_embedded_assets=True,
                    allow_filesystem_fallback=False,
                )
                self.assertEqual(embedded_handler.send_head().read(), b"embedded")
                self.assertEqual(embedded_handler.statuses, [200])

                auto_handler, _ = self._make_tracking_handler(
                    "/index%2Ehtml?debug",
                    root_dir=directory,
                    allow_embedded_assets=True,
                    allow_filesystem_fallback=True,
                )
                self.assertEqual(auto_handler.send_head().read(), b"embedded")

                read_embedded.side_effect = lambda rel_path: None
                fallback_handler, _ = self._make_tracking_handler(
                    "/index.html",
                    root_dir=directory,
                    allow_embedded_assets=True,
                    allow_filesystem_fallback=True,
                )
                self.assertEqual(fallback_handler.send_head().read(), b"directory")

                missing_tracker = slopbro.RequestedFilesTracker(["index.html"])
                embedded_missing, _ = self._make_tracking_handler(
                    "/index.html",
                    tracker=missing_tracker,
                    root_dir=directory,
                    allow_embedded_assets=True,
                    allow_filesystem_fallback=False,
                )
                embedded_missing.do_GET()
                self.assertEqual(embedded_missing.errors[0][0], 404)
                self.assertEqual(missing_tracker.missing_files(), ["index.html"])

            untracked, tracker = self._make_tracking_handler(
                "/other.txt",
                tracker=Mock(),
                tracked_files=("index.html",),
                root_dir=directory,
                allow_embedded_assets=False,
                allow_filesystem_fallback=True,
            )
            untracked.do_GET()
            self.assertEqual(untracked.errors[0][0], 404)
            tracker.mark_seen.assert_not_called()

    def test_payload_response_headers_include_mime_and_length(self):
        handler, _ = self._make_tracking_handler(
            "/main.js", tracked_files=("main.js",),
        )
        with patch.object(slopbro, "_read_embedded_file", return_value=b"let x = 1;"):
            source = handler.send_head()

        self.assertEqual(handler.statuses, [200])
        self.assertEqual(dict(handler.headers)["Content-Length"], "10")
        self.assertIn("javascript", dict(handler.headers)["Content-type"])
        self.assertEqual(source.read(), b"let x = 1;")
        source.close()

    def test_simple_server_get_and_head_responses(self):
        handler_class = slopbro.make_test_connection_handler()
        get_handler = object.__new__(handler_class)
        head_handler = object.__new__(handler_class)
        for handler in (get_handler, head_handler):
            handler.wfile = io.BytesIO()
            handler.statuses = []
            handler.headers = []
            handler.send_response = lambda status, *args, h=handler: h.statuses.append(status)
            handler.send_header = lambda name, value, h=handler: h.headers.append((name, value))
            handler.end_headers = lambda: None

        get_handler.do_GET()
        head_handler.do_HEAD()

        expected_length = str(len(slopbro.TEST_CONNECTION_MESSAGE.encode("utf-8")))
        self.assertEqual(get_handler.wfile.getvalue(), slopbro.TEST_CONNECTION_MESSAGE.encode("utf-8"))
        self.assertEqual(head_handler.wfile.getvalue(), b"")
        self.assertEqual(head_handler.statuses, get_handler.statuses)
        self.assertEqual(head_handler.headers, get_handler.headers)
        self.assertEqual(dict(head_handler.headers)["Content-Length"], expected_length)

    def test_requested_paths(self):
        handler_class = slopbro.make_tracking_handler(
            None, slopbro.RequestedFilesTracker(slopbro.required_files()),
            slopbro.required_files(), True, False,
        )
        handler = object.__new__(handler_class)
        for path, expected in (
            ("/", "index.html"),
            ("/index%2Ehtml?debug", "index.html"),
            ("/caf%C3%A9", "caf\u00e9"),
            ("/%ED%A0%80", "\ud800"),
            ("../missing", None),
        ):
            handler.path = path
            self.assertEqual(handler._requested_rel_path(), expected)

    def test_connection_logging(self):
        handler = object.__new__(slopbro.make_test_connection_handler())
        handler.client_address = ("127.0.0.1", 12345)
        with patch.object(slopbro, "log") as logger:
            handler.log_message("status %s", 200)
        logger.assert_called_once_with("request from 127.0.0.1: status 200")


class Python3CleanupTests(unittest.TestCase):
    def test_websocket_frame_masking(self):
        sock = Mock()
        client = slopbro.WebSocket(sock)
        with patch.object(slopbro.os, "urandom", return_value=b"\x01\x02\x03\x04") as random:
            client._send_frame(client.OP_TEXT, b"abc")
        random.assert_called_once_with(4)
        sock.sendall.assert_called_once_with(b"\x81\x83\x01\x02\x03\x04" + b"\x60" * 3)

    def test_websocket_close_ignores_connection_errors(self):
        for error_type in (OSError, ssl.SSLError, slopbro.WebSocketError):
            with self.subTest(error_type=error_type):
                sock = Mock()
                sock.sendall.side_effect = error_type("disconnected")
                sock.close.side_effect = OSError("already closed")
                client = slopbro.WebSocket(sock)
                client.close()
                self.assertTrue(client.closed)
                sock.close.assert_called_once_with()

    def test_route_lookup_closes_socket(self):
        for failure in (None, "connect", "getsockname"):
            with self.subTest(failure=failure):
                with patch.object(slopbro.socket, "socket") as factory:
                    sock = factory.return_value.__enter__.return_value
                    sock.getsockname.return_value = ("192.168.1.2", 12345)
                    if failure:
                        getattr(sock, failure).side_effect = OSError("no route")
                    result = slopbro._route_selected_local_ip("192.168.1.50", 80)
                self.assertEqual(result, None if failure else "192.168.1.2")
                factory.assert_called_once_with(
                    slopbro.socket.AF_INET, slopbro.socket.SOCK_DGRAM,
                )
                sock.connect.assert_called_once_with(("192.168.1.50", 80))
                factory.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_tls_client_context(self):
        with patch.object(slopbro.socket, "create_connection") as connect:
            with patch.object(ssl.SSLContext, "wrap_socket", autospec=True) as wrap:
                with patch.object(slopbro.WebSocket, "_handshake") as handshake:
                    client = slopbro.WebSocket.connect("tv.local", 3001)
        context = wrap.call_args[0][0]
        self.assertEqual(context.protocol, ssl.PROTOCOL_TLS_CLIENT)
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        wrap.assert_called_once_with(
            context, connect.return_value, server_hostname="tv.local",
        )
        self.assertIs(client._sock, wrap.return_value)
        handshake.assert_called_once_with("tv.local", 3001, True)

    def test_embedded_base64(self):
        for encoded in ("AP9hc3NldA==", b"AP9hc3NldA=="):
            with self.subTest(encoded=encoded):
                with patch.object(slopbro, "EMBEDDED_WWWROOT", {
                    "index.html": {"data": encoded},
                }):
                    self.assertEqual(
                        slopbro._read_embedded_file("index.html"), b"\x00\xffasset",
                    )

    def test_key_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "keys", "tv.key")
            with patch.object(slopbro, "key_path", return_value=path):
                self.assertEqual(slopbro.load_client_key("tv"), "")
                for key in ("first-key", "replacement-key"):
                    slopbro.save_client_key("tv", key)
                    self.assertEqual(slopbro.load_client_key("tv"), key)
                with patch("builtins.open", side_effect=OSError("denied")):
                    with patch.object(slopbro, "log") as logger:
                        slopbro.save_client_key("tv", "unsaved-key")
                    logger.assert_called_once()
                    self.assertIn("could not save client key", logger.call_args[0][0])


if __name__ == "__main__":
    unittest.main()