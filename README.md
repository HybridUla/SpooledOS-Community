# SpooledOS Community

**Build your own touchscreen dash for a DS1-tuned car with a Raspberry Pi and a Waveshare 8.8" screen.**

SpooledOS Community turns a Raspberry Pi 4 and a Waveshare 8.8" DSI touchscreen
into a live gauge cluster that reads your DS1 over WiFi. It's the free,
open-source edition of the software on the [Spooled](https://spooled.racing) dash.

- Live readings laid out for the long screen: a wide boost bar with its target and the last ten seconds,
  and tiles for lambda, fuel pressures, wastegate duty, knock, temps and more, each with a ten-second sparkline
- Hold any tile to rearrange them or change what it shows, from any channel your DS1 streams
- Map switching, with a confirm before every switch and your own name for each map
- Logs every drive and every pull in the DS1's own log format, so they open in the tools tuners already use
- Download logs to your phone over the dash's own WiFi
- Log graphs on the screen
- Fault codes and the check-engine status, with descriptions read from your own DS1
- Ethanol blend calculator for E85 and pump gas
- Updates over WiFi

### What it reads, and what it can change

In the car, SpooledOS Community talks only to your DS1, never to the ECU directly, and it never flashes a tune.
It **reads** your DS1's live data, its fault codes and fault-code descriptions, and its device details.
It can **change** two things on your DS1, and only these:

- **Which map the car runs.** The map page tells your DS1 to switch map slots. A different map is a
  different tune, and the car runs it as soon as the DS1 switches. The dash asks you to confirm every switch.
- **The log mask.** If you set `log_mask` in `settings.json`, the dash writes it to your DS1, which keeps it
  in its flash. The mask picks which channels your DS1 streams and logs. It is only written when it differs
  from what the DS1 already has, and left blank (the default), your DS1 keeps its own.

The **DS1** tab opens your DS1's own web app, which can do anything your DS1 can.

---

## What you need

| Part | Notes |
|------|-------|
| **Raspberry Pi 4** (2 GB or more) | This is what SpooledOS is built and tested on. |
| **Waveshare 8.8" DSI touchscreen** (1920×480, capacitive touch) | The long "bar" screen. It connects to the Pi's DSI port with a ribbon cable. Other screens aren't supported by the installer. |
| **microSD card**, 32 GB or larger | A1 or A2 rated. A good brand matters: the car cuts the power every time you turn it off. |
| **DS1** | Your DS1 joins the dash's WiFi network; the dash reads its live data from there. |
| **12 V to 5 V converter**, 5 A, USB-C output | Powers the Pi and the screen from the car. Don't go smaller than 5 A: an underpowered Pi slows itself down and reboots at random. |
| **Add-a-fuse / fuse tap** for your car's fuse box | Lets you take power from a fuse without cutting any wires. |
| **Inline fuse holder** with a **3 A fuse** | Protects the wire to the dash. |
| **18 AWG wire**, heat-shrink, crimp connectors | For the power run. |
| *Optional:* **DS3231 clock module** | Keeps the time while the car is off, so log names are always dated right. Without it the dash sets its clock from your phone or the internet. |
| *For setup:* a computer, and a USB keyboard or SSH | Only needed once, to run the installer. |

---

## 1. Put Raspberry Pi OS on the card

1. On your computer, install [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Choose **Raspberry Pi 4** as the device, and **Raspberry Pi OS (64-bit)** as the operating system.
   It must be the version **with desktop**. The "Lite" version doesn't have the display system the dash runs on.
3. Choose your microSD card, then **Edit settings** when Imager asks about customisation:
   - set a **username and password** (remember them)
   - enter your **home WiFi** name and password, so the Pi can download SpooledOS
   - under *Services*, turn on **SSH** if you want to install from your computer instead of with a keyboard on the Pi
4. Write the card, then put it in the Pi.

## 2. Connect the screen

1. With the Pi **unplugged**, connect the screen's ribbon cable to the Pi's **DSI display port**
   (the narrow connector labelled *DISPLAY*, beside the micro-HDMI ports).
   Lift the connector's latch, slide the ribbon in fully with the contacts facing the right way, and press the latch down.
2. Connect the screen's power as shown in the instructions that came with it.
3. Plug in the Pi's power. It's normal for the screen to stay **black** at this point: it doesn't light up until the installer has set it up.

## 3. Install SpooledOS

Open a terminal on the Pi (with a keyboard and the Pi plugged into a monitor, or over SSH from your computer:
`ssh yourusername@raspberrypi.local`), then run this one command:

```bash
curl -fsSL https://raw.githubusercontent.com/HybridUla/SpooledOS-Community/main/install.sh | bash
```

It takes a few minutes. It sets up the screen, starts the dash automatically every time the Pi powers on,
and creates a WiFi network for your DS1. When it finishes, restart:

```bash
sudo reboot
```

After the restart the screen goes straight into the dash.

**If you installed over SSH, you'll lose the connection after this reboot.** That's expected: the Pi's WiFi is
now busy being the network your DS1 joins. See [Getting back into the Pi](#getting-back-into-the-pi).

**Options.** To change the WiFi network's name or password, or if you've fitted a DS3231 clock, put the setting
in front of `bash`:

```bash
curl -fsSL https://raw.githubusercontent.com/HybridUla/SpooledOS-Community/main/install.sh | SSID=MyDash PASS=mypassword RTC=1 bash
```

| Setting | Default | What it does |
|---------|---------|--------------|
| `SSID` | `Spooled` | Name of the WiFi network the dash makes for the DS1 |
| `PASS` | `SpooledDash` | Its password, 8 to 63 characters |
| `RTC=1` | off | Turns on a DS3231 clock module on the GPIO header |
| `DIR` | `~/spooledos` | Where SpooledOS is installed |

Running the installer again is safe; it repairs and updates the setup in place.

## 4. Connect your DS1

The first time the dash starts, it walks you through this on the screen:

1. On your DS1, open its WiFi settings.
2. Join the network **Spooled** with the password **SpooledDash** (or the ones you chose).
3. The dash finds the DS1 on its own, and the gauges come alive.

From then on, they connect by themselves every time the car starts.

## 5. Power it from the car

> Work with the car off and the key out. If you're not comfortable working on your car's electrics, have an installer do this part.

```
 fuse box                      inline 3 A fuse        12 V → 5 V converter        Pi
┌──────────────┐  +12 V (switched)  ┌───────┐         ┌────────────────┐  USB-C  ┌──────┐
│ add-a-fuse ──┼────────────────────┤  3 A  ├─────────┤ IN         OUT ├─────────┤ Pi 4 │
└──────────────┘                    └───────┘         │      GND       │         └──────┘
                                                      └───────┬────────┘
                                   chassis ground ────────────┘
```

1. Find a **switched** fuse: one that only has power with the ignition on, so the dash turns off with the car and can't drain the battery.
   Check it with a multimeter or test light: power with the key on, none with the key out.
2. Fit the **add-a-fuse** in that slot. The car's original fuse goes in the lower slot. In the new slot, use a fuse **at least 5 A**.
3. From the add-a-fuse's wire, run 18 AWG wire through the **inline 3 A fuse** to the converter's **+12 V input**.
   Keep the inline fuse close to the fuse box.
4. Connect the converter's **ground** to a clean bare-metal chassis point or a factory ground bolt.
5. Plug the converter's **USB-C** into the Pi.
6. Use crimp connectors or solder with heat-shrink for every joint. Avoid "scotch-lock" T-taps: they loosen with vibration.

Turn the key: the dash boots, and it shuts off with the car.

## Using the dash

- **Gauges.** Swipe or use the arrows to change page. Page 1 is boost, as a wide bar with its target and the
  last ten seconds, beside six tiles. Page 2 is a G-force plot beside six more.
  **Hold any tile** and they all start to wiggle: drag one to a new spot, or tap it to choose what it shows.
  Page 2's wide panel can show any channel too; boost always keeps the one on page 1.
  Tap **Done** when you're finished, or **Reset** to go back to the original layout.
- **Maps.** The third page shows which of your DS1's maps is running. Tap another to switch to it: the dash asks
  you to confirm first, because a different map is a different tune. Name your maps under **Settings → Map names**.
- **Logs.** The dash logs every drive, and saves every full-throttle pull as its own file.
  To get logs onto your phone, join your phone to the **Spooled** WiFi and open **spooled.local/logs**, or scan the QR code under Settings.
- **Diagnostics** shows the check-engine status and fault codes. The descriptions come from your DS1: the dash
  reads its table the first time the two connect, and until then shows each code on its own.
- **Ethanol Blend** works out how much E85 and pump gas to add to hit your target ethanol content.
- **Settings:** brightness, °F/°C, time zone, map names, the DS1 WiFi network, and updates.

## Updates

1. On the dash, go to **Settings → Network** and join your home WiFi (or a phone hotspot).
2. Go to **Settings → Check for updates**, then **Update**. The dash downloads the new version and restarts.
3. Switch back to the DS1's network afterwards from **Settings → Network**.

Or from a terminal on the Pi: `~/spooledos/update.sh`

## Getting back into the Pi

After the first reboot the Pi's WiFi hosts the DS1's network, so it's no longer on your home WiFi. To reach it:

- Join your computer to the **Spooled** WiFi, then `ssh yourusername@10.42.0.1`, or
- on the dash, join your home WiFi from **Settings → Network**, then `ssh yourusername@spooled.local`.

## Try it on your computer

You can run the dash with simulated data, no Pi or car needed. With Python 3 installed:

```bash
git clone https://github.com/HybridUla/SpooledOS-Community.git
cd SpooledOS-Community
python3 server.py --demo
```

Then open **http://localhost:8080** in your browser, with the window sized long and short like the screen.

## Troubleshooting

| Problem | Try this |
|---------|----------|
| **Screen stays black after the reboot** | Check the ribbon cable is fully seated at both ends, contacts facing the right way. Then run the installer again. |
| **Picture is sideways, or touch is in the wrong place** | Run the installer again and reboot. |
| **Dash says the DS1 isn't connected** | Check the DS1 has joined the **Spooled** network. Restart the DS1, then the dash. |
| **Gauges freeze or drop out** | Usually power: use a 5 A converter and a good USB-C cable. **Settings → Debug mode** shows if the Pi is short on power. |
| **Update says "No internet"** | Join the dash to your home WiFi first, from Settings → Network. |
| **Logs page won't open on my phone** | The phone must be on the **Spooled** WiFi. If `spooled.local/logs` won't load, try `10.42.0.1/logs`. |
| **Log times are wrong** | Set your time zone in Settings. Without a DS3231 clock, the time comes from your phone (open the logs page once) or the internet. |

To see what the dash is doing, run `journalctl -u spooled-server -f` in a terminal on the Pi.

## SpooledOS and SpooledOS Community

SpooledOS Community is the free, do-it-yourself edition. The **Spooled** dash from
[spooled.racing](https://spooled.racing) is a ready-built unit that runs **SpooledOS**, which adds
themes, remote support, and automatic log backup to logs.spooled.racing.

## Credits

- **[Dyno Spectrum](https://dynospectrum.com)**, for making the DS1. SpooledOS exists because the DS1
  streams everything a tuned car is doing over WiFi, ready for a screen like this one to read.
- **[opendbc](https://github.com/commaai/opendbc)** by comma.ai, for `vw_mlb.dbc`. The CAN messages the
  dash decodes from the car's bus, and how each signal is laid out and scaled, come from it. Its MIT
  licence is in [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).

Thank you both.

## License

SpooledOS Community is free software: you can redistribute it and/or modify it under the terms of the
**GNU General Public License v3.0**. If you share a modified version, or sell something built on it, you must
release your source code under the same license. See [LICENSE](LICENSE).

SpooledOS Community is not affiliated with or endorsed by Dyno Spectrum, comma.ai, Audi or any car
manufacturer; their names are used only to credit them and to describe what this works with.
It reads data from your DS1, and can switch maps and set the log mask on it
(see [What it reads, and what it can change](#what-it-reads-and-what-it-can-change)).
Fit and use it at your own risk, and keep your eyes on the road.
