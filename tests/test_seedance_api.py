import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from api.routes.generation import (
    SEEDANCE_MODEL_ID,
    SEEDANCE_NEGATIVE_PROMPT,
    _parse_seedance_request,
)
from core.adobe_client import AdobeClient, UpstreamTemporaryError


class SeedanceApiTest(unittest.TestCase):
    def test_firefly_resolution_and_ratio_matrix(self):
        expected = {
            "480p": {
                "21:9": (1120, 480),
                "16:9": (854, 480),
                "4:3": (640, 480),
                "1:1": (480, 480),
                "3:4": (480, 640),
                "9:16": (480, 854),
            },
            "720p": {
                "21:9": (1680, 720),
                "16:9": (1280, 720),
                "4:3": (960, 720),
                "1:1": (720, 720),
                "3:4": (720, 960),
                "9:16": (720, 1280),
            },
            "1080p": {
                "21:9": (2520, 1080),
                "16:9": (1920, 1080),
                "4:3": (1440, 1080),
                "1:1": (1080, 1080),
                "3:4": (1080, 1440),
                "9:16": (1080, 1920),
            },
        }
        for resolution, ratios in expected.items():
            for ratio, (width, height) in ratios.items():
                with self.subTest(resolution=resolution, ratio=ratio):
                    request = _parse_seedance_request(
                        {
                            "model": SEEDANCE_MODEL_ID,
                            "content": [{"type": "text", "text": "test"}],
                            "resolution": resolution,
                            "ratio": ratio,
                            "duration": 4,
                        }
                    )
                    self.assertEqual(request["resolution"], resolution)
                    self.assertEqual(request["ratio"], ratio)
                    self.assertEqual(
                        AdobeClient._video_size(ratio, resolution),
                        {"width": width, "height": height},
                    )

    def test_firefly_duration_and_official_default_noops(self):
        for duration in (4, 5, 10, 15):
            with self.subTest(duration=duration):
                request = _parse_seedance_request(
                    {
                        "model": SEEDANCE_MODEL_ID,
                        "content": [{"type": "text", "text": "test"}],
                        "duration": duration,
                        "camera_fixed": False,
                        "draft": False,
                        "return_last_frame": False,
                        "service_tier": "default",
                        "priority": 0,
                        "tools": [],
                    }
                )
                self.assertEqual(request["duration"], duration)
                self.assertEqual(request["resolution"], "720p")
                self.assertEqual(request["ratio"], "16:9")

        for name, value in (
            ("resolution", "4k"),
            ("ratio", "adaptive"),
            ("duration", -1),
            ("duration", 16),
            ("watermark", True),
            ("return_last_frame", True),
            ("priority", 1),
            ("service_tier", "flex"),
            ("tools", [{"type": "web_search"}]),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(HTTPException):
                    _parse_seedance_request(
                        {
                            "model": SEEDANCE_MODEL_ID,
                            "content": [{"type": "text", "text": "test"}],
                            name: value,
                        }
                    )

    def test_multimodal_references_map_to_firefly_blobs(self):
        request = _parse_seedance_request(
            {
                "model": SEEDANCE_MODEL_ID,
                "content": [
                    {
                        "type": "text",
                        "text": "Use @Image1, @Video1 and @Audio1.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/ref.png"},
                        "role": "reference_image",
                    },
                    {
                        "type": "video_url",
                        "video_url": {"url": "https://example.com/ref.mp4"},
                        "role": "reference_video",
                    },
                    {
                        "type": "audio_url",
                        "audio_url": {"url": "https://example.com/ref.wav"},
                        "role": "reference_audio",
                    },
                ],
            }
        )
        self.assertEqual(len(request["reference_images"]), 1)
        self.assertEqual(
            request["reference_media"],
            [
                {"kind": "video", "url": "https://example.com/ref.mp4"},
                {"kind": "audio", "url": "https://example.com/ref.wav"},
            ],
        )

        ids = [Mock(hex=char * 32) for char in ("a", "b", "c")]
        client = object.__new__(AdobeClient)
        with patch("core.adobe_client.uuid.uuid4", side_effect=ids):
            payload = client._build_video_payload(
                video_conf={"engine": "seedance2", "resolution": "720p"},
                prompt=request["prompt"],
                aspect_ratio=request["ratio"],
                duration=request["duration"],
                reference_image_ids=["image-id"],
                reference_video_ids=["video-id"],
                reference_audio_ids=["audio-id"],
            )

        self.assertEqual(
            payload["referenceBlobs"],
            [
                {
                    "id": "image-id",
                    "usage": "asset",
                    "mention": {"id": "a" * 21, "label": "Image1"},
                },
                {
                    "id": "video-id",
                    "usage": "source",
                    "mention": {"id": "b" * 21, "label": "Video1"},
                },
                {
                    "id": "audio-id",
                    "usage": "source",
                    "mention": {"id": "c" * 21, "label": "Audio1"},
                },
            ],
        )
        self.assertEqual(
            payload["prompt"],
            f"Use @{'a' * 21}, @{'b' * 21} and @{'c' * 21}.",
        )

    def test_upload_media_uses_firefly_storage_endpoint(self):
        client = object.__new__(AdobeClient)
        client.user_agent = "test-agent"
        client.sec_ch_ua = '"Chromium";v="145"'
        client.upload_url = "https://example.com/v2/storage/image"
        response = Mock(status_code=200, text="")
        response.json.return_value = {"assets": [{"id": "video-id"}]}

        with patch.object(client, "_post_bytes", return_value=response) as post_bytes:
            media_id = client.upload_media(
                "token", b"video", "video/mp4", "video"
            )

        self.assertEqual(media_id, "video-id")
        self.assertEqual(
            post_bytes.call_args.args[0],
            "https://example.com/v2/storage/video",
        )
        headers = post_bytes.call_args.kwargs["headers"]
        self.assertEqual(headers["x-api-key"], "clio-playground-web")
        self.assertEqual(headers["content-type"], "video/mp4")

    def test_seedance_uses_firefly_headers_once_for_upstream_408(self):
        client = object.__new__(AdobeClient)
        client.api_key = "projectx_webapp"
        client.user_agent = "test-agent"
        client.sec_ch_ua = '"Chromium";v="143"'
        client.video_submit_url = "https://example.com/video"
        response = Mock(
            status_code=408,
            text='{"error_code":"timeout_error","message":"system under load"}',
            headers={},
        )
        client._post_json = Mock(return_value=response)

        with self.assertRaises(UpstreamTemporaryError) as raised:
            client.generate_video(
                token="token",
                video_conf={"engine": "seedance2", "resolution": "480p"},
                prompt="苹果变青色",
                aspect_ratio="16:9",
                duration=5,
            )

        self.assertEqual(raised.exception.status_code, 408)
        self.assertEqual(client._post_json.call_count, 1)
        headers = client._post_json.call_args.kwargs["headers"]
        self.assertEqual(headers["origin"], "https://firefly.adobe.com")
        self.assertEqual(headers["x-api-key"], "clio-playground-web")

    def test_seedance_reuses_legacy_video_submit_headers(self):
        client = object.__new__(AdobeClient)
        client.api_key = "projectx_webapp"
        client.user_agent = "test-agent"
        client.sec_ch_ua = '"Chromium";v="143"'
        client.upload_url = "https://example.com/upload"
        token = "token"
        response = Mock(status_code=200, text="")
        response.json.return_value = {"images": [{"id": "image-id"}]}

        with unittest.mock.patch.object(
            client, "_post_bytes", return_value=response
        ) as post_bytes:
            headers = client._video_submit_headers(token)
            image_id = client.upload_image(
                token, b"image", "image/jpeg", firefly=True
            )

        upload_headers = post_bytes.call_args.kwargs["headers"]

        self.assertEqual(image_id, "image-id")
        self.assertEqual(headers["x-api-key"], "projectx_webapp")
        self.assertEqual(headers["origin"], "https://new.express.adobe.com")
        self.assertEqual(headers["referer"], "https://new.express.adobe.com/")
        self.assertNotIn("x-arp-session-id", headers)
        self.assertNotIn("x-nonce", headers)
        self.assertEqual(upload_headers["x-api-key"], "clio-playground-web")
        self.assertEqual(upload_headers["origin"], "https://firefly.adobe.com")
        self.assertEqual(upload_headers["content-type"], "image/jpeg")
        self.assertNotIn("x-nonce", upload_headers)
        self.assertNotIn("x-arp-session-id", upload_headers)

    def test_seedance_reports_451_as_upstream_rejection(self):
        client = object.__new__(AdobeClient)
        client.user_agent = "test-agent"
        client.sec_ch_ua = '"Chromium";v="143"'
        client.video_submit_url = "https://example.com/video"
        response = Mock(
            status_code=451,
            text='{"error_code":"legal_error","message":"{}"}',
            headers={},
        )
        client._post_json = Mock(return_value=response)

        with self.assertRaises(UpstreamTemporaryError) as raised:
            client.generate_video(
                token="token",
                video_conf={"engine": "seedance2", "resolution": "480p"},
                prompt="苹果变青色",
                aspect_ratio="16:9",
                duration=5,
            )

        self.assertEqual(raised.exception.status_code, 451)
        self.assertEqual(raised.exception.error_type, "status")
        self.assertEqual(client._post_json.call_count, 1)

    def test_official_request_maps_to_captured_adobe_payload(self):
        request = _parse_seedance_request(
            {
                "model": SEEDANCE_MODEL_ID,
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/last.jpg"},
                        "role": "last_frame",
                    },
                    {"type": "text", "text": "摘下苹果"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/first.jpg"},
                        "role": "first_frame",
                    },
                ],
                "resolution": "480p",
                "ratio": "16:9",
                "duration": 5,
                "seed": 202381,
                "generate_audio": False,
            }
        )
        self.assertEqual(
            [item["image_url"]["url"] for item in request["images"]],
            ["https://example.com/first.jpg", "https://example.com/last.jpg"],
        )

        client = object.__new__(AdobeClient)
        payload = client._build_video_payload(
            video_conf={"engine": "seedance2", "resolution": "480p"},
            prompt=request["prompt"],
            aspect_ratio=request["ratio"],
            duration=request["duration"],
            source_image_ids=["first-id", "last-id"],
            negative_prompt=SEEDANCE_NEGATIVE_PROMPT,
            generate_audio=request["generate_audio"],
            seed=request["seed"],
        )
        self.assertEqual(
            payload,
            {
                "modelId": "seedance",
                "modelVersion": "seedance_2.0",
                "size": {"width": 854, "height": 480},
                "seeds": [202381],
                "referenceBlobs": [
                    {"id": "first-id", "usage": "frame", "order": 1},
                    {"id": "last-id", "usage": "frame", "order": 2},
                ],
                "prompt": "摘下苹果",
                "negativePrompt": SEEDANCE_NEGATIVE_PROMPT,
                "duration": 5,
                "generateAudio": False,
                "generationMetadata": {
                    "module": "text2video",
                    "submodule": "ff-video-generate",
                },
                "generationSettings": {"aspectRatio": "16:9"},
                "output": {"storeInputs": True},
            },
        )


if __name__ == "__main__":
    unittest.main()
