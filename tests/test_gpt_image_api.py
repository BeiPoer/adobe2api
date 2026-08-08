import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from fastapi import Request

from api.routes.generation import build_generation_router
from core.adobe_client import AdobeClient
from core.models.catalog import MODEL_CATALOG
from core.models.payloads import (
    build_image_payload_candidates,
    parse_gpt_image_n,
    parse_gpt_image_size,
)
from core.models.resolver import resolve_model, resolve_ratio_and_resolution


class GptImageApiTests(unittest.TestCase):
    def test_request_limits(self):
        self.assertEqual(parse_gpt_image_size("auto"), {"width": 1024, "height": 1024})
        self.assertEqual(
            parse_gpt_image_size("3840x2160"),
            {"width": 3840, "height": 2160},
        )
        for size in ("800x800", "1024x1025", "3840x1264", "3840x3840", "invalid"):
            with self.subTest(size=size), self.assertRaises(ValueError):
                parse_gpt_image_size(size)

        self.assertEqual(parse_gpt_image_n(None), 1)
        self.assertEqual(parse_gpt_image_n("10"), 10)
        for value in (0, 11, 1.5, True, "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_gpt_image_n(value)

    def test_alias_payload_does_not_change_legacy_payload(self):
        alias = build_image_payload_candidates(
            prompt="test",
            aspect_ratio="1:1",
            output_resolution="1K",
            upstream_model_id="gpt-image",
            upstream_model_version="2",
            detail_level=3,
            pixel_size={"width": 1376, "height": 768},
            n=2,
        )[0]
        self.assertEqual(alias["size"], {"width": 1376, "height": 768})
        self.assertEqual(alias["modelSpecificPayload"], {})
        self.assertEqual(alias["generationSettings"], {"detailLevel": 3})
        self.assertEqual(alias["n"], 2)
        self.assertEqual(len(alias["seeds"]), 2)
        self.assertNotIn("outputResolution", alias)

        legacy = build_image_payload_candidates(
            prompt="test",
            aspect_ratio="16:9",
            output_resolution="2K",
            upstream_model_id="gpt-image",
            upstream_model_version="2",
            quality_level="high",
        )[0]
        self.assertEqual(legacy["size"], {"width": 2560, "height": 1440})
        self.assertEqual(legacy["modelSpecificPayload"], {"size": "2560x1440"})
        self.assertEqual(legacy["outputResolution"], "2K")
        self.assertEqual(legacy["generationSettings"], {"detailLevel": 5})
        self.assertEqual(legacy["n"], 1)

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

    def test_generation_route_maps_alias_options_and_response_formats(self):
        class Client:
            gpt_image_quality = "high"
            generate_timeout = 30

            def __init__(self):
                self.calls = []

            def generate(self, **kwargs):
                self.calls.append(kwargs)
                return [b"one", b"two"], {}

        class ExpectedError(Exception):
            pass

        client = Client()
        unused = lambda *args, **kwargs: None

        with TemporaryDirectory() as temp_dir:
            router = build_generation_router(
                store=None,
                token_manager=None,
                client=client,
                generated_dir=Path(temp_dir),
                model_catalog=MODEL_CATALOG,
                video_model_catalog={},
                supported_ratios=set(),
                resolve_model=resolve_model,
                resolve_ratio_and_resolution=resolve_ratio_and_resolution,
                require_service_api_key=unused,
                set_request_task_progress=unused,
                run_with_token_retries=lambda **kwargs: kwargs["run_once"]("token"),
                set_request_error_detail=lambda *args, **kwargs: "error",
                set_request_preview=unused,
                public_image_url=lambda request, image_id: f"/generated/{image_id}.png",
                public_generated_url=lambda request, file_name: f"/generated/{file_name}",
                resolve_video_options=unused,
                load_input_images=unused,
                load_input_videos=unused,
                load_input_audios=unused,
                prepare_video_source_image=unused,
                video_ext_from_meta=unused,
                extract_prompt_from_messages=unused,
                sse_chat_stream=unused,
                on_generated_file_written=unused,
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
            data = {
                "model": "gpt-image-2",
                "prompt": "test",
                "size": "1376x768",
                "quality": "high",
                "n": 2,
            }

            b64_response = endpoint(data, request)
            self.assertEqual(
                [item["b64_json"] for item in b64_response["data"]],
                [
                    base64.b64encode(b"one").decode("ascii"),
                    base64.b64encode(b"two").decode("ascii"),
                ],
            )

            url_response = endpoint({**data, "response_format": "url"}, request)
            self.assertEqual(len(url_response["data"]), 2)
            self.assertEqual(
                [path.read_bytes() for path in sorted(Path(temp_dir).glob("*.png"))],
                [b"one", b"two"],
            )

        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertEqual(call["pixel_size"], {"width": 1376, "height": 768})
            self.assertEqual(call["detail_level"], 3)
            self.assertIsNone(call["quality_level"])
            self.assertEqual(call["n"], 2)
            self.assertIsNone(call["out_path"])


if __name__ == "__main__":
    unittest.main()
