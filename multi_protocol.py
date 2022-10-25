import logging
import os
from typing import Optional

import grpc
from docarray import Document
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from jina import Client, Executor, Flow, __default_host__, requests
from jina.helper import get_full_version
from jina.importer import ImportExtensions
from jina.proto import jina_pb2, jina_pb2_grpc
from jina.serve.gateway import BaseGateway
from jina.serve.runtimes.gateway.http.app import get_fastapi_app
from jina.serve.runtimes.helper import _get_grpc_server_options
from jina.types.request.status import StatusMessage


class HTTPGRPCGateway(BaseGateway):
    """HTTP Gateway implementation"""

    def __init__(
        self,
        grpc_port: Optional[int] = None,
        http_port: Optional[int] = None,
        grpc_server_options: Optional[dict] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        no_debug_endpoints: Optional[bool] = False,
        no_crud_endpoints: Optional[bool] = False,
        expose_endpoints: Optional[str] = None,
        expose_graphql_endpoint: Optional[bool] = False,
        cors: Optional[bool] = False,
        ssl_keyfile: Optional[str] = None,
        ssl_certfile: Optional[str] = None,
        uvicorn_kwargs: Optional[dict] = None,
        proxy: Optional[bool] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.grpc_port = grpc_port
        self.http_port = http_port
        self.grpc_server_options = grpc_server_options
        self.title = title
        self.description = description
        self.no_debug_endpoints = no_debug_endpoints
        self.no_crud_endpoints = no_crud_endpoints
        self.expose_endpoints = expose_endpoints
        self.expose_graphql_endpoint = expose_graphql_endpoint
        self.cors = cors
        self.ssl_keyfile = ssl_keyfile
        self.ssl_certfile = ssl_certfile
        self.uvicorn_kwargs = uvicorn_kwargs
        self.health_servicer = health.HealthServicer(experimental_non_blocking=True)

        if not proxy and os.name != 'nt':
            os.unsetenv('http_proxy')
            os.unsetenv('https_proxy')

    async def setup_http_server(self):
        """
        Initialize and return GRPC server
        """
        from jina.helper import extend_rest_interface

        self.app = extend_rest_interface(
            get_fastapi_app(
                streamer=self.streamer,
                title=self.title,
                description=self.description,
                no_debug_endpoints=self.no_debug_endpoints,
                no_crud_endpoints=self.no_crud_endpoints,
                expose_endpoints=self.expose_endpoints,
                expose_graphql_endpoint=self.expose_graphql_endpoint,
                cors=self.cors,
                logger=self.logger,
                tracing=self.tracing,
                tracer_provider=self.tracer_provider,
            )
        )

        with ImportExtensions(required=True):
            from uvicorn import Config, Server

        class UviServer(Server):
            """The uvicorn server."""

            async def setup(self, sockets=None):
                """
                Setup uvicorn server.

                :param sockets: sockets of server.
                """
                config = self.config
                if not config.loaded:
                    config.load()
                self.lifespan = config.lifespan_class(config)
                self.install_signal_handlers()
                await self.startup(sockets=sockets)
                if self.should_exit:
                    return

            async def serve(self, **kwargs):
                """
                Start the server.

                :param kwargs: keyword arguments
                """
                await self.main_loop()

        if 'CICD_JINA_DISABLE_HEALTHCHECK_LOGS' in os.environ:

            class _EndpointFilter(logging.Filter):
                def filter(self, record: logging.LogRecord) -> bool:
                    # NOTE: space is important after `GET /`, else all logs will be disabled.
                    return record.getMessage().find("GET / ") == -1

            # Filter out healthcheck endpoint `GET /`
            logging.getLogger("uvicorn.access").addFilter(_EndpointFilter())

        uvicorn_kwargs = self.uvicorn_kwargs or {}

        if self.ssl_keyfile and 'ssl_keyfile' not in uvicorn_kwargs.keys():
            uvicorn_kwargs['ssl_keyfile'] = self.ssl_keyfile

        if self.ssl_certfile and 'ssl_certfile' not in uvicorn_kwargs.keys():
            uvicorn_kwargs['ssl_certfile'] = self.ssl_certfile

        self.http_server = UviServer(
            config=Config(
                app=self.app,
                host=__default_host__,
                port=self.http_port,
                log_level=os.getenv('JINA_LOG_LEVEL', 'error').lower(),
                **uvicorn_kwargs,
            )
        )

        await self.http_server.setup()

    async def setup_grpc_server(self):
        """
        setup GRPC server
        """
        self.grpc_server = grpc.aio.server(
            options=_get_grpc_server_options(self.grpc_server_options),
            interceptors=self.grpc_tracing_server_interceptors,
        )

        jina_pb2_grpc.add_JinaRPCServicer_to_server(
            self.streamer._streamer, self.grpc_server
        )

        jina_pb2_grpc.add_JinaGatewayDryRunRPCServicer_to_server(self, self.grpc_server)
        jina_pb2_grpc.add_JinaInfoRPCServicer_to_server(self, self.grpc_server)

        service_names = (
            jina_pb2.DESCRIPTOR.services_by_name['JinaRPC'].full_name,
            jina_pb2.DESCRIPTOR.services_by_name['JinaGatewayDryRunRPC'].full_name,
            jina_pb2.DESCRIPTOR.services_by_name['JinaInfoRPC'].full_name,
            reflection.SERVICE_NAME,
        )
        # Mark all services as healthy.
        health_pb2_grpc.add_HealthServicer_to_server(
            self.health_servicer, self.grpc_server
        )

        for service in service_names:
            self.health_servicer.set(service, health_pb2.HealthCheckResponse.SERVING)
        reflection.enable_server_reflection(service_names, self.grpc_server)

        bind_addr = f'{__default_host__}:{self.grpc_port}'

        if self.ssl_keyfile and self.ssl_certfile:
            with open(self.ssl_keyfile, 'rb') as f:
                private_key = f.read()
            with open(self.ssl_certfile, 'rb') as f:
                certificate_chain = f.read()

            server_credentials = grpc.ssl_server_credentials(
                (
                    (
                        private_key,
                        certificate_chain,
                    ),
                )
            )
            self.grpc_server.add_secure_port(bind_addr, server_credentials)
        elif (
            self.ssl_keyfile != self.ssl_certfile
        ):  # if we have only ssl_keyfile and not ssl_certfile or vice versa
            raise ValueError(
                f"you can't pass a ssl_keyfile without a ssl_certfile and vice versa"
            )
        else:
            self.grpc_server.add_insecure_port(bind_addr)
        self.logger.debug(f'start server bound to {bind_addr}')
        await self.grpc_server.start()

    async def setup_server(self):
        await self.setup_grpc_server()
        await self.setup_http_server()

    async def teardown(self):
        """
        Free resources allocated when setting up HTTP server
        """
        await super().teardown()
        await self.http_server.shutdown()
        self.health_servicer.enter_graceful_shutdown()

    async def stop_server(self):
        """
        Stop HTTP server
        """
        self.http_server.should_exit = True
        await self.grpc_server.stop(0)

    async def run_server(self):
        """Run HTTP server forever"""
        await self.http_server.serve()
        await self.grpc_server.wait_for_termination()

    async def dry_run(self, empty, context) -> jina_pb2.StatusProto:
        """
        Process the the call requested by having a dry run call to every Executor in the graph

        :param empty: The service expects an empty protobuf message
        :param context: grpc context
        :returns: the response request
        """
        from docarray import DocumentArray

        from jina.clients.request import request_generator
        from jina.enums import DataInputType
        from jina.serve.executors import __dry_run_endpoint__

        da = DocumentArray()

        try:
            req_iterator = request_generator(
                exec_endpoint=__dry_run_endpoint__,
                data=da,
                data_type=DataInputType.DOCUMENT,
            )
            async for _ in self.streamer.stream(request_iterator=req_iterator):
                pass
            status_message = StatusMessage()
            status_message.set_code(jina_pb2.StatusProto.SUCCESS)
            return status_message.proto
        except Exception as ex:
            status_message = StatusMessage()
            status_message.set_exception(ex)
            return status_message.proto

    async def _status(self, empty, context) -> jina_pb2.JinaInfoProto:
        """
        Process the the call requested and return the JinaInfo of the Runtime

        :param empty: The service expects an empty protobuf message
        :param context: grpc context
        :returns: the response request
        """
        infoProto = jina_pb2.JinaInfoProto()
        version, env_info = get_full_version()
        for k, v in version.items():
            infoProto.jina[k] = str(v)
        for k, v in env_info.items():
            infoProto.envs[k] = str(v)
        return infoProto

    @property
    def should_exit(self) -> bool:
        """
        Boolean flag that indicates whether the gateway server should exit or not
        :return: boolean flag
        """
        return self.http_server.should_exit


class MyExecutor(Executor):
    @requests
    def foo(self, *args, **kwargs):
        print('received')


flow = Flow(
    uses='HTTPGRPCGateway', uses_with={'grpc_port': 12345, 'http_port': 12346}
).add(uses='MyExecutor')


with flow as f:
    grpc_client = Client(protocol='grpc', port=12345)
    http_client = Client(protocol='http', port=12346)
    grpc_client.post(on='/', inputs=[Document()])
    http_client.post(on='/', inputs=[Document()])
