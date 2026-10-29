import asyncio
from unittest.mock import AsyncMock

import pytest

from app.models.integration import IntegrationConfig
from app.services.feature_settings import FeatureSettingsService
from app.features.catalog import get_feature
from tests.helpers import make_settings, login_client
from fastapi.testclient import TestClient
from app.main import create_app


def test_vision_encrypted_masked_and_reset(client):
    response = client.put('/api/v1/integrations/vision', json={
        'base_url': 'https://example.com/v1/', 'model': 'vision-model', 'api_key': 'secret-test-key'})
    assert response.status_code == 200
    view = response.json()['vision']
    assert view['available']
    assert view['base_url']['value'] == 'https://example.com/v1'
    assert 'secret-test-key' not in response.text
    assert view['api_key']['masked'].startswith('****')
    assert view['supports_vision']['value'] is True
    assert view['supports_vision']['source'] == 'unset'
    with client.app.state.session_factory() as session:
        row = session.get(IntegrationConfig, 'vision.api_key')
        assert b'secret-test-key' not in row.value_ciphertext
    assert client.delete('/api/v1/integrations/vision').json()['vision']['available'] is False


def test_vision_supports_vision_roundtrip(client):
    response = client.put(
        '/api/v1/integrations/vision',
        json={'supports_vision': False},
    )
    assert response.status_code == 200, response.text
    view = response.json()['vision']
    assert view['supports_vision'] == {
        'value': False,
        'source': 'db',
        'editable': True,
    }
    response = client.put(
        '/api/v1/integrations/vision',
        json={'supports_vision': True},
    )
    assert response.json()['vision']['supports_vision']['value'] is True


def test_vision_test_uses_text_when_support_disabled(client, monkeypatch):
    import httpx

    client.put(
        '/api/v1/integrations/vision',
        json={
            'base_url': 'https://example.com/v1',
            'model': 'text-model',
            'api_key': 'secret',
            'supports_vision': False,
        },
    )
    calls = []

    def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200,
            json={'choices': [{'message': {'content': 'OK'}}]},
            request=httpx.Request('POST', url),
        )

    monkeypatch.setattr(httpx.Client, 'post', post)
    response = client.request('POST', '/api/v1/integrations/vision/test')
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['data'] == {'mode': 'text', 'supports_vision': False}
    assert calls
    content = calls[0][1]['json']['messages'][0]['content']
    assert isinstance(content, str)
    assert 'image_url' not in str(calls[0][1]['json'])
    assert 'secret' not in response.text


def test_vision_env_lock(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path, vision_api_key='env-secret')))
    login_client(client)
    assert client.put('/api/v1/integrations/vision', json={'api_key': 'new'}).status_code == 409
    assert client.delete('/api/v1/integrations/vision').status_code == 409
    assert 'env-secret' not in client.get('/api/v1/integrations').text


def test_vision_supports_vision_env_lock(tmp_path):
    client = TestClient(
        create_app(make_settings(tmp_path, vision_supports_vision=False))
    )
    login_client(client)
    view = client.get('/api/v1/integrations').json()['vision']
    assert view['supports_vision'] == {
        'value': False,
        'source': 'env',
        'editable': False,
    }
    assert client.put(
        '/api/v1/integrations/vision',
        json={'supports_vision': True},
    ).status_code == 409


@pytest.mark.parametrize('url', ['https://example.com', 'https://user:secret@example.com/v1', 'https://example.com/v1?key=secret'])
def test_vision_invalid_url(client, url):
    response = client.put('/api/v1/integrations/vision', json={'base_url': url})
    assert response.status_code == 422
    assert 'secret' not in response.text


def test_policy_defaults_constraints_relaxation_and_reset(client):
    path = '/api/v1/features/action.click/settings'
    assert client.put(path, json={'defaults': {}, 'required_fields': []}).status_code == 422
    assert client.put(path, json={'defaults': {'selector': []}, 'required_fields': []}).status_code == 422
    assert client.put(path, json={'defaults': {'click_count': 9}, 'required_fields': ['selector']}).status_code == 422
    response = client.put(path, json={'defaults': {'selector': ['#button']}, 'required_fields': []})
    assert response.status_code == 200
    view = response.json()
    assert view['base_required_fields'] == ['selector']
    assert view['required_fields'] == []
    assert view['customized']
    assert view['config_schema']['properties']['selector']['default'] == ['#button']
    with client.app.state.session_factory() as session:
        assert FeatureSettingsService(session).validate(get_feature('action.click'), {})['selector'] == ['#button']
    assert client.delete(path).json()['customized'] is False
    views = client.get('/api/v1/features').json()
    assert next(v for v in views if v['key'] == 'auth.form_login')['defaults']['username_selector']


def test_worker_vision_method_uses_vision(monkeypatch):
    from worker import executor, vision

    monkeypatch.setattr(vision, 'recognize', lambda *args: '1234')
    monkeypatch.setattr(
        executor,
        '_classify_captcha',
        lambda image: (_ for _ in ()).throw(AssertionError('ddddocr must be skipped')),
    )
    image = AsyncMock()
    image.screenshot.return_value = b'cropped-png'
    logs = []
    result = asyncio.run(executor._read_captcha(
        None,
        image,
        {'captcha_method': 'vision', 'charset': 'digits', 'length': 4},
        lambda level, message: logs.append(message),
        {
            'base_url': 'https://example.com/v1',
            'model': 'vision',
            'api_key': 'secret',
            'execution_settings': {'llm_prompt': 'read'},
        },
    ))
    assert result[0] == 'ok'
    assert result[1] == '1234'
    assert result[3]['provider'] == 'vision'
    assert 'confidence' not in result[3]
    assert 'secret' not in str(logs)


def test_worker_vision_method_does_not_fall_back(monkeypatch):
    from worker import executor, vision

    monkeypatch.setattr(vision, 'recognize', lambda *args: 'wrong')
    monkeypatch.setattr(
        executor,
        '_classify_captcha',
        lambda image: (_ for _ in ()).throw(AssertionError('ddddocr must be skipped')),
    )

    async def no_refresh(*_args):
        return None

    monkeypatch.setattr(executor, '_refresh_captcha_image', no_refresh)
    image = AsyncMock()
    image.screenshot.return_value = b'cropped-png'
    result = asyncio.run(executor._read_captcha(
        None,
        image,
        {'captcha_method': 'vision', 'charset': 'digits', 'length': 4},
        lambda *args: None,
        {
            'base_url': 'https://example.com/v1',
            'model': 'vision',
            'api_key': 'secret',
            'execution_settings': {'llm_prompt': 'read'},
        },
    ))
    assert result[0] == 'restart'
    assert result[3]['provider'] == 'vision'
    assert '字符集不符合' in result[3]['reason']


def test_worker_captcha_uses_ddddocr_by_default_and_ignores_vision(monkeypatch):
    from worker import executor, vision

    monkeypatch.setattr(
        vision,
        'recognize',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('vision must be skipped')
        ),
    )
    monkeypatch.setattr(executor, '_classify_captcha', lambda image: ('5678', .99))
    image = AsyncMock()
    image.screenshot.return_value = b'cropped-png'
    result = asyncio.run(
        executor._read_captcha(
            None,
            image,
            {'charset': 'digits', 'length': 4},
            lambda *args: None,
            {
                'base_url': 'https://example.com/v1',
                'model': 'text-model',
                'api_key': 'secret',
                'supports_vision': False,
                'execution_settings': {'llm_prompt': 'read'},
            },
        )
    )
    assert result[0] == 'ok'
    assert result[1] == '5678'
    assert result[3].get('provider') == 'ddddocr'


def test_worker_vision_method_requires_vision_support(monkeypatch):
    from worker import executor

    monkeypatch.setattr(
        executor,
        '_classify_captcha',
        lambda image: (_ for _ in ()).throw(AssertionError('ddddocr must be skipped')),
    )
    image = AsyncMock()
    image.screenshot.return_value = b'cropped-png'
    result = asyncio.run(executor._read_captcha(
        None,
        image,
        {'captcha_method': 'vision', 'charset': 'digits', 'length': 4},
        lambda *args: None,
        {
            'base_url': 'https://example.com/v1',
            'model': 'text-model',
            'api_key': 'secret',
            'supports_vision': False,
        },
    ))
    assert result[0] == 'fail'
    assert result[3]['reason'] == 'vision_not_supported'


def test_image_quiz_skips_when_vision_capability_off(monkeypatch):
    from worker import executor

    messages: list[str] = []
    result = asyncio.run(
        executor._run_image_quiz(
            None,
            {},
            lambda level, message: messages.append(message),
            {
                'base_url': 'https://example.com/v1',
                'model': 'text-model',
                'api_key': 'secret',
                'supports_vision': False,
            },
        )
    )
    assert result[0] == 'waiting_human'
    assert result[2]['reason'] == 'vision_not_supported'


def test_vision_test_posts_image_and_sanitizes(client, monkeypatch):
    import httpx
    client.put('/api/v1/integrations/vision', json={
        'base_url': 'https://example.com/v1', 'model': 'vision', 'api_key': 'secret'})
    calls = []
    def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'white pixel'}}]},
                              request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    # Use request(), since Client.post is patched for the provider.
    assert client.request('POST', '/api/v1/integrations/vision/test').json()['ok']
    assert calls[0][0].endswith('/v1/chat/completions')
    assert calls[0][1]['json']['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    def fail(*args, **kwargs):
        raise RuntimeError('secret provider body')
    monkeypatch.setattr(httpx.Client, 'post', fail)
    response = client.request('POST', '/api/v1/integrations/vision/test')
    assert not response.json()['ok']
    assert 'secret' not in response.text


def test_standalone_worker_openai_response(monkeypatch):
    import io
    import json
    from worker import vision
    requests = []
    def open_request(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': ' Ab12 '}}]}).encode())
    monkeypatch.setattr(vision, 'urlopen', open_request)
    assert vision.recognize(b'cropped', {'base_url': 'https://example.com/v1',
        'api_key': 'secret', 'model': 'vision', 'timeout': 12}, 'read') == 'Ab12'
    request, timeout = requests[0]
    assert request.full_url == 'https://example.com/v1/chat/completions'
    assert timeout == 12
    assert json.loads(request.data)['messages'][0]['content'][1]['image_url']['url'].endswith('Y3JvcHBlZA==')


def test_worker_vision_does_not_forward_secret_on_redirect():
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from worker import vision

    leaked_headers = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            leaked_headers.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with ExitStack() as stack:
        destination = stack.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Destination))

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{destination.server_port}/capture')
                self.end_headers()

            def log_message(self, *args):
                pass

        source = stack.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Redirect))
        for server in (destination, source):
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
        with pytest.raises(ValueError, match='Vision provider request failed'):
            vision.recognize(b'cropped', {
                'base_url': f'http://127.0.0.1:{source.server_port}/v1',
                'api_key': 'secret', 'model': 'vision',
            }, 'read')
        assert leaked_headers == []


@pytest.mark.parametrize('message,finish,answer', [
    ({'content': '<think>Possible code 9999. Check image.</think> Ab12'}, 'stop', 'Ab12'),
    ({'reasoning_content': '9999', 'content': 'Ab12'}, 'stop', 'Ab12'),
    ({'content': [{'type': 'text', 'text': '<think>inspect</think>'},
                  {'type': 'text', 'text': 'Ab12'}]}, 'stop', 'Ab12'),
    ({'reasoning_content': 'Ab12', 'content': None}, 'stop', None),
    ({'content': '<think>Ab12</think>'}, 'stop', None),
    ({'content': '<think>Ab12'}, 'stop', None),
    ({'content': 'Ab12</think>'}, 'stop', None),
    ({'content': '<think><think>inspect</think>Ab12'}, 'stop', None),
    ({'content': 'The captcha is Ab12'}, 'stop', None),
    ({'content': 'Ab12'}, 'length', None),
])
def test_reasoning_response_parsing_for_vision_method(
    monkeypatch, message, finish, answer
):
    import io
    import json
    from worker import executor, vision

    monkeypatch.setattr(vision, 'urlopen', lambda *args, **kwargs: io.BytesIO(json.dumps(
        {'choices': [{'message': message, 'finish_reason': finish}]}).encode()))

    async def no_refresh(*_args):
        return None

    monkeypatch.setattr(executor, '_refresh_captcha_image', no_refresh)
    image = AsyncMock()
    image.screenshot.return_value = b'cropped'
    result = asyncio.run(executor._read_captcha(None, image,
        {'captcha_method': 'vision', 'charset': 'alnum', 'length': 4},
        lambda *args: None,
        {'base_url': 'https://example.com/v1', 'model': 'vision', 'api_key': 'secret',
         'execution_settings': {'llm_prompt': 'read'}}))
    assert result[3].get('provider') == 'vision'
    if answer:
        assert result[0] == 'ok'
        assert result[1] == answer
    else:
        assert result[0] == 'fail'
        assert result[1] == ''
        assert result[3]['reason'] == 'provider_failed'


def test_image_quiz_feature_is_available(client):
    from app.features.base import IMAGE_QUIZ_DEFAULT_PROMPT

    views = client.get('/api/v1/features').json()
    feature = next(item for item in views if item['key'] == 'sign.image_quiz')
    assert 'vision' in feature['requires']
    assert feature['category'] == 'sign'
    props = feature['config_schema']['properties']
    assert props['image_selector']['default']
    assert props['answer_selector']['default'] == [
        "input[type='submit'][name^='captcha_']"
    ]
    assert props['max_refresh']['default'] == 3
    assert feature['execution_settings']['llm_prompt'] == IMAGE_QUIZ_DEFAULT_PROMPT


def test_image_captcha_method_catalog(client):
    views = client.get('/api/v1/features').json()
    for key in ('challenge.image_captcha', 'sign.iframe.image_captcha'):
        feature = next(item for item in views if item['key'] == key)
        assert 'vision' not in feature['requires']
        method = feature['config_schema']['properties']['captcha_method']
        assert method['default'] == 'ddddocr'
        assert method['enum'] == ['ddddocr', 'vision', 'nexusphp_gd5']
        assert method['enum_titles']['vision'] == '视觉模型识别'
        assert 'NexusPHP' in method['enum_descriptions']['nexusphp_gd5']
        assert feature['defaults']['captcha_method'] == 'ddddocr'
        assert feature['execution_settings'] == {
            'llm_prompt': (
                'Read the captcha in this image. Return only its characters, '
                'without explanation, spaces, or Markdown. Preserve letter case.'
            )
        }


def test_image_quiz_prompt_execution_settings_save_and_reset(client):
    from app.features.base import IMAGE_QUIZ_DEFAULT_PROMPT

    key = 'sign.image_quiz'
    path = f'/api/v1/features/{key}/settings'
    view = next(v for v in client.get('/api/v1/features').json() if v['key'] == key)
    payload = {
        name: view[name]
        for name in ('defaults', 'required_fields', 'execution_settings')
    }
    payload['execution_settings']['llm_prompt'] = '请识别图片中的作品并选择对应选项。'
    response = client.put(path, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['execution_settings']['llm_prompt'] == payload[
        'execution_settings'
    ]['llm_prompt']
    assert client.delete(path).json()['execution_settings']['llm_prompt'] == (
        IMAGE_QUIZ_DEFAULT_PROMPT
    )


@pytest.mark.parametrize(
    ("message", "finish", "expected"),
    [
        ({"content": '{"is_clear": true, "selected_index": 2}'}, "stop", 2),
        (
            {
                "reasoning_content": "thinking",
                "content": '{"is_clear": true, "selected_index": 1}',
            },
            "stop",
            1,
        ),
        (
            {
                "content": '<think>inspect</think>'
                '{"is_clear": true, "selected_index": 2}'
            },
            "stop",
            2,
        ),
        (
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"is_clear": true, "selected_index": 1}',
                    }
                ]
            },
            "stop",
            1,
        ),
        ({"reasoning_content": "only reasoning", "content": None}, "stop", "error"),
        ({"content": "The answer is 2"}, "stop", "error"),
        ({"content": '{"is_clear": true, "selected_index": 9}'}, "stop", "error"),
        ({"content": '{"is_clear": true, "selected_index": 1}'}, "length", "error"),
        ({"content": '{"is_clear": false, "selected_index": 1}'}, "stop", "unclear"),
        (
            {
                "content": '{"is_clear": true, "selected_index": 1, '
                '"need_refresh": true}'
            },
            "stop",
            "unclear",
        ),
    ],
)
def test_image_quiz_vision_response_parsing(monkeypatch, message, finish, expected):
    import io
    import json
    from worker import vision

    monkeypatch.setattr(
        vision,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(
            json.dumps(
                {"choices": [{"message": message, "finish_reason": finish}]}
            ).encode()
        ),
    )
    config = {
        "base_url": "https://vision.example/v1",
        "model": "vision",
        "api_key": "secret",
    }
    try:
        selected, _reason = vision.answer_image_quiz(
            b"image", "题目", ["A", "B"], config
        )
    except vision.ImageQuizUnclear:
        assert expected == "unclear"
    except ValueError:
        assert expected == "error"
    else:
        assert selected == expected


def test_image_captcha_execution_settings_save_again(client):
    import json
    from app.models.setting import AppSetting

    key = 'challenge.image_captcha'
    path = f'/api/v1/features/{key}/settings'
    view = next(v for v in client.get('/api/v1/features').json() if v['key'] == key)
    payload = {name: view[name] for name in ('defaults', 'required_fields', 'execution_settings')}
    payload['defaults']['captcha_method'] = 'nexusphp_gd5'
    payload['execution_settings']['llm_prompt'] = 'Read the image.'
    for _ in range(2):
        response = client.put(path, json=payload)
        assert response.status_code == 200, response.text
        for name, value in payload.items():
            assert response.json()[name] == value
        view = next(v for v in client.get('/api/v1/features').json() if v['key'] == key)
        assert view['execution_settings'] == payload['execution_settings']
        with client.app.state.session_factory() as session:
            stored = json.loads(session.get(AppSetting, f'feature.{key}').value_json)
            assert stored == payload


def test_execution_policy_contract(client):
    import pytest
    from app.features.base import CAPTCHA_DEFAULT_PROMPT
    from app.features.base import FeatureConfigError

    key = 'challenge.image_captcha'
    path = f'/api/v1/features/{key}/settings'
    view = next(v for v in client.get('/api/v1/features').json() if v['key'] == key)
    assert view['execution_settings'] == {'llm_prompt': CAPTCHA_DEFAULT_PROMPT}
    assert view['defaults']['captcha_method'] == 'ddddocr'
    for field in ('prefer_llm', 'llm_prompt'):
        assert field not in view['defaults']
        assert field not in view['config_schema']['properties']
        assert field not in view['required_fields']
    payload = {'defaults': {'length': 5}, 'required_fields': [],
               'execution_settings': {'llm_prompt': 'managed prompt'}}
    assert client.put(path, json=payload).json()['execution_settings'] == payload['execution_settings']
    with client.app.state.session_factory() as session:
        service = FeatureSettingsService(session)
        feature = get_feature(key)
        config = service.validate(feature, {})
        assert config['length'] == 5
        assert config['captcha_method'] == 'ddddocr'
        assert 'prefer_llm' not in config and 'llm_prompt' not in config
        for field in ('prefer_llm', 'llm_prompt'):
            with pytest.raises(FeatureConfigError):
                service.validate(feature, {field: True})
            with pytest.raises(FeatureConfigError):
                feature.validate_override({field: True})
    assert client.put(path, json={'defaults': {'prefer_llm': True}, 'required_fields': []}).status_code == 422
    assert client.put(path, json={
        'defaults': {'length': 5}, 'required_fields': [],
        'execution_settings': {'prefer_llm': True}}).status_code == 422
    assert client.put('/api/v1/features/action.click/settings', json={
        'defaults': {'selector': ['button']}, 'required_fields': [],
        'execution_settings': {'llm_prompt': 'managed prompt'}}).status_code == 422
    reset = client.delete(path).json()
    assert reset['execution_settings'] == {'llm_prompt': CAPTCHA_DEFAULT_PROMPT}
    assert reset['defaults']['captcha_method'] == 'ddddocr'
    assert reset['customized'] is False
