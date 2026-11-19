// AIRUX default desktop: a Windows 11 style taskbar.
//   [ spacer | Start (AIRUX) | pinned and running apps | spacer | tray | clock | show desktop ]

// wallpaper on every screen
var desktops = desktopsForActivity(currentActivity());
for (var i = 0; i < desktops.length; i++) {
    var d = desktops[i];
    d.wallpaperPlugin = "org.kde.image";
    d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
    d.writeConfig("Image", "file:///usr/share/backgrounds/airux/airux-wallpaper.png");
}

var panel = new Panel;
panel.location = "bottom";
panel.height = 2 * Math.floor(gridUnit * 2.7 / 2);   // about 48 px, like Windows 11
panel.floating = false;
panel.hiding = "none";

panel.addWidget("org.kde.plasma.panelspacer");

var start = panel.addWidget("org.kde.plasma.kickoff");
start.currentConfigGroup = ["General"];
start.writeConfig("icon", "aira");
// pinned Start apps: Kickoff's default list is set in the image (see 9000-airux.hook.chroot)
start.writeConfig("primaryActions", "0");     // Start menu: apps first, power buttons at the bottom
start.writeConfig("showActionButtonCaptions", "false");

var tasks = panel.addWidget("org.kde.plasma.icontasks");
tasks.currentConfigGroup = ["General"];
tasks.writeConfig("launchers", [
    "applications:org.kde.dolphin.desktop", "applications:chromium.desktop", "applications:airox-code.desktop",
    "applications:org.kde.konsole.desktop", "applications:systemsettings.desktop", "applications:aira.desktop"
].join(","));
tasks.writeConfig("iconSpacing", "2");

panel.addWidget("org.kde.plasma.panelspacer");

panel.addWidget("org.kde.plasma.systemtray");

var clock = panel.addWidget("org.kde.plasma.digitalclock");
clock.currentConfigGroup = ["Appearance"];
clock.writeConfig("showDate", "true");
clock.writeConfig("dateDisplayFormat", "BelowTime");
clock.writeConfig("dateFormat", "shortDate");

panel.addWidget("org.kde.plasma.showdesktop");
