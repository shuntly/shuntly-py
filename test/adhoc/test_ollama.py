import io
import json
import os

import ollama
import pytest

from shuntly import SinkStream, shunt

_API_KEY = os.environ.get('OLLAMA_API_KEY')
_MODEL = 'nemotron-3-nano:30b'

pytestmark = pytest.mark.skipif(not _API_KEY, reason='OLLAMA_API_KEY not set')


def _make_client():
    return ollama.Client(
        host='https://ollama.com',
        headers={'Authorization': f'Bearer {_API_KEY}'},
    )


def test_wrap_captures_chat_record():
    buf = io.StringIO()
    client = shunt(_make_client(), SinkStream(buf))

    response = client.chat(
        model=_MODEL,
        messages=[{'role': 'user', 'content': 'Reply with just the word: pong'}],
    )

    assert 'message' in response
    assert 'content' in response['message']

    record = json.loads(buf.getvalue().strip())

    assert record['client'] == 'ollama._client.Client'
    assert record['method'] == 'chat'
    assert record['request']['model'] == _MODEL
    assert record['error'] is None
    assert record['duration_ms'] > 0
    assert 'message' in record['response']
    assert record['response']['message']['content'].strip() == 'pong'


def test_wrap_captures_generate_record():
    buf = io.StringIO()
    client = shunt(_make_client(), SinkStream(buf))

    response = client.generate(
        model=_MODEL,
        prompt='Reply with just the word: ping',
    )

    assert 'response' in response

    record = json.loads(buf.getvalue().strip())

    assert record['client'] == 'ollama._client.Client'
    assert record['method'] == 'generate'
    assert record['request']['model'] == _MODEL
    assert record['request']['prompt'] == 'Reply with just the word: ping'
    assert record['error'] is None
    assert record['duration_ms'] > 0
    assert 'response' in record['response']
    assert record['response']['response'].strip() == 'ping'


def test_wrap_captures_streaming_chat():
    buf = io.StringIO()
    client = shunt(_make_client(), SinkStream(buf))

    chunks = []
    stream = client.chat(
        model=_MODEL,
        messages=[
            {'role': 'user', 'content': 'Reply with just the words: ping pong ping pong'}
        ],
        stream=True,
    )

    for chunk in stream:
        chunks.append(chunk)

    assert len(chunks) > 0
    assert all('message' in chunk for chunk in chunks)

    record = json.loads(buf.getvalue().strip())

    assert record['client'] == 'ollama._client.Client'
    assert record['method'] == 'chat'
    assert record['request']['model'] == _MODEL
    assert record['request']['stream'] is True
    assert record['error'] is None
    assert record['duration_ms'] > 0
    assert isinstance(record['response'], list)
    assert len(record['response']) > 0

    responses = record['response']
    post = ''.join(r['message']['content'].strip() for r in responses)
    assert post == 'pingpongpingpong'
