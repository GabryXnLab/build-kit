# Modulo Android: JDK 17, cargo-ndk e un SDK/NDK minimale (nessun emulatore né system image: l'SDK completo per
# le prove a schermo sta nel kit agent-skills/ohos-emulator). Con `sdk` (un derivation con libexec/android-sdk,
# es. quello del kit) si usa quello invece di comporne uno.
{ pkgs, sdk ? null, buildTools ? [ "36.0.0" ], platforms ? [ "36" ], ndk ? [ "27.3.13750724" ], cmake ? [ "3.22.1" ] }:
let
  composed = pkgs.androidenv.composeAndroidPackages {
    buildToolsVersions = buildTools;
    platformVersions = platforms;
    ndkVersions = ndk;
    cmakeVersions = cmake;
    includeNDK = true;
    includeEmulator = false;
    includeSystemImages = false;
  };
  androidsdk = if sdk != null then sdk else composed.androidsdk;
  home = "${androidsdk}/libexec/android-sdk";
  ndkVersion = builtins.head ndk;
in
{
  packages = [ pkgs.jdk17 pkgs.cargo-ndk pkgs.android-tools ];
  env = {
    JAVA_HOME = "${pkgs.jdk17}";
    ANDROID_HOME = home;
    ANDROID_SDK_ROOT = home;
    ANDROID_NDK_HOME = "${home}/ndk/${ndkVersion}";
    # AAPT2 scaricato da Maven è un ELF dinamico di Ubuntu: si usa quello del build-tools dell'SDK.
    GRADLE_OPTS = "-Dorg.gradle.project.android.aapt2FromMavenOverride=${home}/build-tools/${builtins.head buildTools}/aapt2";
  };
}
