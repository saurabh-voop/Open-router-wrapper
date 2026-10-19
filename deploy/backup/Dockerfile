FROM postgres:16.10-alpine
RUN apk add --no-cache aws-cli tzdata
COPY backup.sh /usr/local/bin/relay-backup
RUN chmod 0755 /usr/local/bin/relay-backup
ENTRYPOINT ["/usr/local/bin/relay-backup"]
CMD ["loop"]
