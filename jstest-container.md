# jstest in a container

```bash
# Build once
podman build -t jstest-local -f Containerfile.jstest .

# Run — press buttons while it runs, Ctrl-C to stop
podman run --rm --privileged --device /dev/input/js0 jstest-local jstest --event /dev/input/js0
```

`--privileged` is required because `js0` maps to `nobody:nobody` inside the
container due to podman's user namespace remapping.
