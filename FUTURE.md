# Tasks

* Help me understand how to unlock high sample rates (192kHz). Just setting it in the program does not seem to be enough, the read data is still 48kHz. Add guide to readme file

# Future ideas

## Support modern Mac and Windows

## Reimplement in rust

## Arduino version

## Run as daemon, detect when PPM signal is present

## Trainer mode

Make a plan to add profile autogeneration mode. This mode will guide the user to generate a profile for their controller. It will listen to the configured or autodetected audio source for PPM data. Once channel count is locked, print general findings about the data stream. Then guide the user to configure each of the channels: starting from channel one and going up, help the user set up each channel. Ask the user to go through the full width of the channel (e.g. press button, move slider, sweep axis). Ask the user which kind of control this is (axis, slider, button), to what virtual joystick output to map it, whether to invert the axis, etc. At the end, ask for device name and store profile. Make this mode compatible with other already present command line arguments and features.