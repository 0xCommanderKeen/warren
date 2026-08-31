# Deployment

Observatory is a static single-page application. Build it with `pnpm build` and publish the contents of `dist/`.

The web server owns three routing rules:

1. Serve static files from the build directory.
2. Fall back to `index.html` for browser routes such as `/agents/:uuid`.
3. Proxy `/state` and `/state/stream` to Burrow without buffering the stream.

An nginx site can use this shape:

```nginx
location / {
    try_files $uri $uri/ /index.html;
}

location = /state {
    proxy_pass http://127.0.0.1:8737/state$is_args$args;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    add_header Cache-Control no-store always;
}

location = /state/stream {
    proxy_pass http://127.0.0.1:8737/state/stream$is_args$args;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 1h;
    add_header X-Accel-Buffering no always;
    add_header Cache-Control no-store always;
}
```

Observatory may instead connect directly to a CORS-enabled Burrow deployment through the `backend` query parameter. Same-origin proxying is preferred because it avoids exposing a second public origin and keeps browser routing independent of API location.
