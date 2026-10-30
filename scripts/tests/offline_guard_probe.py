"""Subprocess probe: install the benchmark's offline guard through one entry
point, then prove every sync and asyncio network path is refused before any
packet while the event loop, to_thread and the self-pipe keep working.

Usage: offline_guard_probe.py <benchmark|qualifier script> <cache_root> <output_root>
"""

import asyncio
import importlib.util
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path


def load(script):
    spec = importlib.util.spec_from_file_location(Path(script).stem.replace("-", "_"), script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def refused(label, attempt):
    try:
        attempt()
    except OSError as error:
        assert "forbids" in str(error), f"{label}: refused for another reason: {error}"
    else:
        raise AssertionError(f"{label}: admitted")


async def asyncio_connect(address):
    await asyncio.open_connection(*address)


async def asyncio_datagram(address):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, remote_addr=address)
    try:
        transport.sendto(b"probe")
        await asyncio.sleep(0.05)
    finally:
        transport.close()


async def asyncio_sock_connect(address):
    with socket.socket() as stream:
        await asyncio.get_running_loop().sock_connect(stream, address)


def main(script, cache_root, output_root):
    module = load(script)
    bench = getattr(module, "bench", module)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    accepted = []
    threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True).start()
    local = listener.getsockname()
    remote = ("192.0.2.1", 443)

    settings = bench.Settings("qwen_custom_voice", "pytorch", "cpu", "1.7B", "en", "Test",
                              Path(cache_root), Path(output_root), preset_voice="Ryan")
    output = bench.Output(settings.output_root)
    bench.configure_offline(settings, output)
    assert os.environ["HF_HUB_OFFLINE"] == os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert os.environ["HF_HUB_CACHE"] == str(settings.cache_root)
    assert Path(tempfile.gettempdir()).is_relative_to(output.path)
    assert not any(name.startswith("backend") for name in sys.modules)

    refused("getaddrinfo", lambda: socket.getaddrinfo("example.invalid", 443))
    refused("gethostbyname", lambda: socket.gethostbyname("example.invalid"))
    refused("gethostbyaddr", lambda: socket.gethostbyaddr("192.0.2.1"))
    refused("sync connect remote", lambda: socket.create_connection(remote))
    refused("sync connect local listener", lambda: socket.create_connection(local))

    def datagram(address):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"probe", address)

    refused("sync datagram", lambda: datagram(remote))
    refused("asyncio open_connection local listener", lambda: asyncio.run(asyncio_connect(local)))
    refused("asyncio open_connection remote", lambda: asyncio.run(asyncio_connect(remote)))
    refused("asyncio datagram endpoint", lambda: asyncio.run(asyncio_datagram(local)))
    refused("asyncio sock_connect", lambda: asyncio.run(asyncio_sock_connect(local)))
    listener.close()
    assert not accepted, "a connection reached the local listener"

    left, right = socket.socketpair()
    left.close()
    right.close()
    assert asyncio.run(asyncio.sleep(0, result="loop ran")) == "loop ran"
    assert asyncio.run(asyncio.to_thread(lambda: "thread ran")) == "thread ran"
    output.close()
    print("offline guard passed")


if __name__ == "__main__":
    main(*sys.argv[1:4])
