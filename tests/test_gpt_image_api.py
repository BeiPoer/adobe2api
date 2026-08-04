import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi import Request

from api.routes.generation import build_generation_router
from core.adobe_client import AdobeClient
from core.models.catalog import MODEL_CATALOG
from core.models.payloads import (
    build_image_payload_candidates,
    gpt_image_detail_level_from_quality,
    parse_gpt_image_n,
    parse_gpt_image_size,
)
from core.refresh_mgr import RefreshManager


class GptImageApiTests(unittest.TestCase):
    def test_alias_and_legacy_model_coexist(self):
        self.assertEqual(MODEL_CATALOG["gpt-image-2"]["upstream_model_version"], "2")
        legacy = MODEL_CATALOG["firefly-gpt-image-2k-16x9"]
        self.assertEqual((legacy["output_resolution"], legacy["aspect_ratio"]), ("2K", "16:9"))

    def test_size_uses_openai_limits(self):
        self.assertEqual(parse_gpt_image_size("auto"), {"width": 1024, "height": 1024})
        self.assertEqual(parse_gpt_image_size("3840x2160"), {"width": 3840, "height": 2160})
        for size in ("800x800", "1024x1025", "3840x1264", "3840x3840", "invalid"):
            with self.subTest(size=size), self.assertRaises(ValueError):
                parse_gpt_image_size(size)

    def test_quality_maps_to_detail_level_without_changing_legacy_default(self):
        self.assertEqual(
            [gpt_image_detail_level_from_quality(value) for value in ("low", "medium", "high")],
            [1, 3, 3],
        )
        self.assertEqual(gpt_image_detail_level_from_quality(None), 1)
        self.assertEqual(gpt_image_detail_level_from_quality("legacy-value"), 1)

    def test_n_uses_openai_limits_and_reaches_adobe_payload(self):
        self.assertEqual(parse_gpt_image_n(None), 1)
        self.assertEqual(parse_gpt_image_n("10"), 10)
        for value in (0, 11, 1.5, True, "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_gpt_image_n(value)
        payload = self._payload(n=4)[0]
        self.assertEqual(payload["n"], 4)
        self.assertEqual(len(payload["seeds"]), 4)

    def test_client_returns_all_requested_images(self):
        class Response:
            status_code = 200
            headers = {}
            text = ""

            def __init__(self, data=None, content=b""):
                self.data = data or {}
                self.content = content

            def json(self):
                return self.data

            def raise_for_status(self):
                return None

        client = object.__new__(AdobeClient)
        client.submit_url = "submit"
        client._build_payload_candidates = lambda **kwargs: [{"n": kwargs["n"]}]
        client._submit_headers = lambda *args, **kwargs: {}
        client._poll_headers = lambda *args, **kwargs: {}
        client._post_json = lambda *args, **kwargs: Response(
            {"links": {"result": {"href": "poll"}}}
        )
        responses = {
            "poll": Response(
                {
                    "outputs": [
                        {"image": {"presignedUrl": "image-1"}},
                        {"image": {"presignedUrl": "image-2"}},
                    ]
                }
            ),
            "image-1": Response(content=b"one"),
            "image-2": Response(content=b"two"),
        }
        client._get = lambda url, **kwargs: responses[url]

        images, _meta = client.generate(token="token", prompt="test", n=2)
        self.assertEqual(images, [b"one", b"two"])

    def test_url_response_returns_all_requested_images(self):
        class Client:
            gpt_image_quality = "low"
            generate_timeout = 30

            def generate(self, **kwargs):
                self.generate_kwargs = kwargs
                return [b"one", b"two"], {}

        class ExpectedError(Exception):
            pass

        client = Client()
        written = []
        previews = []
        unused = lambda *args, **kwargs: None

        with TemporaryDirectory() as temp_dir:
            router = build_generation_router(
                store=None,
                token_manager=None,
                client=client,
                generated_dir=Path(temp_dir),
                model_catalog={"gpt-image-2": {}},
                video_model_catalog={},
                supported_ratios=set(),
                resolve_model=lambda model_id: {
                    "upstream_model_id": "gpt-image",
                    "upstream_model_version": "2",
                    "detail_level": 3,
                },
                resolve_ratio_and_resolution=lambda data, model_id: (
                    "9:16",
                    "2K",
                    "gpt-image-2",
                ),
                require_service_api_key=unused,
                set_request_task_progress=unused,
                run_with_token_retries=lambda **kwargs: kwargs["run_once"]("token"),
                set_request_error_detail=lambda *args, **kwargs: "error",
                set_request_preview=lambda request, url, kind: previews.append((url, kind)),
                public_image_url=lambda request, image_id: f"/generated/{image_id}.png",
                public_generated_url=lambda request, file_name: f"/generated/{file_name}",
                resolve_video_options=unused,
                load_input_images=unused,
                prepare_video_source_image=unused,
                video_ext_from_meta=unused,
                extract_prompt_from_messages=unused,
                sse_chat_stream=unused,
                on_generated_file_written=lambda path, old_size, new_size: written.append(
                    (path, old_size, new_size)
                ),
                quota_error_cls=ExpectedError,
                auth_error_cls=ExpectedError,
                upstream_temp_error_cls=ExpectedError,
                logger=Mock(),
            )
            endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/v1/images/generations"
            )
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/images/generations",
                    "headers": [],
                    "scheme": "http",
                    "server": ("testserver", 80),
                    "client": ("testclient", 123),
                    "query_string": b"",
                }
            )
            response = endpoint(
                {
                    "model": "gpt-image-2",
                    "prompt": "test",
                    "n": 2,
                    "size": "768x1376",
                    "response_format": "url",
                },
                request,
            )

            self.assertEqual(len(response["data"]), 2)
            self.assertTrue(response["data"][0]["url"].endswith("-1.png"))
            self.assertTrue(response["data"][1]["url"].endswith("-2.png"))
            self.assertEqual(
                [path.read_bytes() for path in sorted(Path(temp_dir).glob("*.png"))],
                [b"one", b"two"],
            )
            self.assertEqual(client.generate_kwargs["n"], 2)
            self.assertIsNone(client.generate_kwargs["out_path"])
            self.assertEqual(len(written), 2)
            self.assertEqual(previews, [(response["data"][0]["url"], "image")])

    def test_image_submit_headers_include_firefly_session_values(self):
        client = object.__new__(AdobeClient)
        client._browser_headers = lambda: {}
        with patch("core.adobe_client._build_submit_nonce", return_value="nonce"), patch(
            "core.adobe_client._arp_session_id_for_token", return_value="arp-session"
        ):
            headers = client._submit_headers("token", prompt="test")

        self.assertEqual(headers["origin"], "https://firefly.adobe.com")
        self.assertEqual(headers["referer"], "https://firefly.adobe.com/")
        self.assertEqual(headers["x-api-key"], "clio-playground-web")
        self.assertEqual(headers["x-nonce"], "nonce")
        self.assertEqual(headers["x-arp-session-id"], "arp-session")

    def test_image_submit_headers_do_not_forge_missing_arp_session(self):
        client = object.__new__(AdobeClient)
        client._browser_headers = lambda: {}
        with patch("core.adobe_client._build_submit_nonce", return_value=""), patch(
            "core.adobe_client._arp_session_id_for_token", return_value=""
        ):
            headers = client._submit_headers("token", prompt="test")

        self.assertNotIn("x-arp-session-id", headers)

    def test_exported_cookie_bundle_keeps_cookie_and_arp_header(self):
        bundle = {
            "cookie": "first=one; second=two",
            "headers": {"x-arp-session-id": "official-session"},
        }

        self.assertEqual(
            RefreshManager._cookie_string_from_input(bundle),
            "first=one; second=two",
        )
        self.assertEqual(
            RefreshManager._firefly_headers_from_input(bundle),
            {"x-arp-session-id": "official-session"},
        )

    def test_refresh_bundle_uses_firefly_identity(self):
        bundle = RefreshManager._validate_bundle(
            {
                "endpoint": {
                    "url": RefreshManager.DEFAULT_REFRESH_URL,
                    "form": {
                        "client_id": RefreshManager.DEFAULT_CLIENT_ID,
                        "scope": "AdobeID,firefly_api,openid",
                    },
                    "headers": {"Cookie": "first=one"},
                }
            }
        )["endpoint"]

        self.assertEqual(bundle["form"]["client_id"], "clio-playground-web")
        self.assertIn("creative_production", bundle["form"]["scope"])
        self.assertEqual(bundle["headers"]["Origin"], "https://firefly.adobe.com")

    def test_cookie_refresh_identity_follows_arp_header(self):
        manager = object.__new__(RefreshManager)
        manager._lock = nullcontext()
        manager._profiles = []
        manager._save_profiles = lambda: None

        legacy = manager.import_cookie({"cookie": "ims_sid=legacy"})
        firefly = manager.import_cookie(
            {
                "cookie": "ims_sid=firefly",
                "headers": {"x-arp-session-id": "official-session"},
            }
        )

        self.assertEqual(
            legacy["endpoint"]["client_id"], RefreshManager.LEGACY_CLIENT_ID
        )
        self.assertEqual(
            firefly["endpoint"]["client_id"], RefreshManager.DEFAULT_CLIENT_ID
        )
        migrated = RefreshManager._normalize_stored_profile(
            {**manager._profiles[1], "firefly_headers": {}}, 0
        )
        self.assertEqual(
            migrated["endpoint"]["form"]["client_id"],
            RefreshManager.LEGACY_CLIENT_ID,
        )

    @patch("core.refresh_mgr.token_manager.upsert_auto_refresh_token")
    def test_cookie_import_preserves_captured_browser_token(self, upsert):
        manager = object.__new__(RefreshManager)
        manager._lock = nullcontext()
        manager._profiles = []
        manager._save_profiles = lambda: None
        token = "header." + ("a" * 120) + ".signature"

        profile = manager.import_cookie(
            {
                "cookie": "ims_sid=firefly",
                "access_token": token,
                "headers": {"x-arp-session-id": "official-session"},
            },
            name="browser",
        )

        self.assertTrue(profile["access_token_imported"])
        upsert.assert_called_once_with(
            token,
            profile_id=profile["id"],
            profile_name="browser",
            profile_email="",
        )

    def test_custom_generation_payload_uses_exact_size(self):
        payload = self._payload(pixel_size={"width": 1376, "height": 768})[0]
        self.assertEqual(payload["size"], {"width": 1376, "height": 768})
        self.assertEqual(payload["modelSpecificPayload"], {})
        self.assertEqual(payload["generationSettings"], {"detailLevel": 3})
        self.assertNotIn("outputResolution", payload)

    def test_edit_payload_matches_adobe_capture_and_legacy_stays_unchanged(self):
        payload = self._payload(
            pixel_size={"width": 1376, "height": 768},
            source_image_ids=["image-id"],
        )[0]
        self.assertEqual(payload["referenceBlobs"], [{"id": "image-id", "usage": "subject"}])
        self.assertEqual(payload["modelSpecificPayload"], {})
        self.assertEqual(payload["generationMetadata"]["module"], "text2image")
        self.assertNotIn("outputResolution", payload)

        legacy = self._payload(
            pixel_size=None,
            source_image_ids=["image-id"],
            aspect_ratio="16:9",
            output_resolution="2K",
        )[0]
        self.assertEqual(legacy["size"], {"width": 2560, "height": 1440})
        self.assertEqual(legacy["outputResolution"], "2K")

    @staticmethod
    def _payload(**overrides):
        values = {
            "prompt": "test",
            "aspect_ratio": "1:1",
            "output_resolution": "1K",
            "upstream_model_id": "gpt-image",
            "upstream_model_version": "2",
            "detail_level": 3,
        }
        values.update(overrides)
        return build_image_payload_candidates(**values)


if __name__ == "__main__":
    unittest.main()
