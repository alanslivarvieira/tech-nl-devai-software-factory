# Build from the repository root:  docker build -f langgraph-factory/Dockerfile -t factory-app .
FROM factory-sandbox:latest
COPY langgraph-factory /opt/factory/langgraph-factory
COPY examples /opt/factory/examples
RUN pip install --no-cache-dir /opt/factory/langgraph-factory
WORKDIR /opt/factory
ENV FACTORY_WORKSPACES=/var/factory/workspaces PORT=8080
EXPOSE 8080
CMD ["factory-app"]
