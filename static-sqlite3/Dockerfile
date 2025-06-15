# Use a minimal Alpine Linux image
FROM alpine:latest

# Set environment variables (optional, but good practice)
ENV HOME=/root

# Install build dependencies
# build-base includes gcc, make, musl-dev
# wget, tar for downloading and extracting source
# bash for script execution
RUN apk add --no-cache build-base wget tar bash

# Create a directory to output the binary
RUN mkdir /out

# Copy the build script into the container.
# Since the Dockerfile itself is in 'src', 'build.sh' is directly in its context.
COPY build.sh /usr/local/bin/build.sh
RUN chmod +x /usr/local/bin/build.sh

# Set the entrypoint to the build script, allowing arguments to be passed
ENTRYPOINT ["/usr/local/bin/build.sh"]