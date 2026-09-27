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
            curl_insecure=False,
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
                curl_insecure=True,
            )

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
            )
        self.runners["run"].assert_not_called()

    def test_help_aliases(self):
        for option in ("--help", "-h", "-?"):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                with self.assertRaises(SystemExit) as caught:
                    slopbro.main([option])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("--test-server", output.getvalue())
        for runner in self.runners.values():
            runner.assert_not_called()

    def test_invalid_arguments(self):
        invalid = [
            [], ["tv", "extra"], ["--unknown"],
            ["--asset-source=bad", "tv"], ["--test-server=bad"],
            ["--local-ip=bad", "tv"], ["--local-ip=", "tv"],
            ["--webos-version=", "tv"], ["--webos-version", " ", "tv"],
        ]
        invalid.extend([[option] for option in (
            "--local-ip", "--webos-version", "--asset-source", "--test-server",
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