"""Loopback forwarder for the fixture dashboard in a container.

The dashboard binds 127.0.0.1 only (house rule), which Docker's port publish
cannot reach, so this listens on 0.0.0.0:3000 inside the container and pipes
each connection to the dashboard on 127.0.0.1:4712. Docker publishes 3000 to
the host's loopback; host Playwright then reaches http://127.0.0.1:3000.
"""
import asyncio
import os

LISTEN = int(os.environ.get("FORWARD_PORT", "3000"))
TARGET = int(os.environ.get("GRAVEDECAY_PORT", "4712"))


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def handle(client_reader, client_writer):
    try:
        server_reader, server_writer = await asyncio.open_connection("127.0.0.1", TARGET)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(pipe(client_reader, server_writer), pipe(server_reader, client_writer))


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
