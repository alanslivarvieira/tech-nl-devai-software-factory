# Build from the repository root:  docker build -f langgraph-factory/Dockerfile -t factory-app .
# (build langgraph-factory/sandbox/Dockerfile as factory-sandbox first; docker compose does this automatically)
FROM factory-sandbox
COPY langgraph-factory /opt/factory/langgraph-factory
COPY examples /opt/factory/examples
RUN pip install --no-cache-dir "/opt/factory/shared[teams]" /opt/factory/langgraph-factory
# Let git clone private repos and push PR branches with $GITHUB_TOKEN (read at run time, never baked in).
RUN git config --system credential.https://github.com.helper \
    '!f() { test "$1" = get && test -n "$GITHUB_TOKEN" && echo username=x-access-token && echo "password=$GITHUB_TOKEN"; }; f'
WORKDIR /opt/factory
ENV FACTORY_WORKSPACES=/var/factory/workspaces FACTORY_EXAMPLES_DIR=/opt/factory/examples PORT=8080
EXPOSE 8080
CMD ["factory-app"]
