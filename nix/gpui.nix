# Librerie di GPUI a runtime su NixOS, comuni alla shell di sviluppo (nix/mkshell.nix) e a un eventuale
# pacchetto con makeWrapper. GPUI apre Vulkan, Wayland e X11 con dlopen e su NixOS non stanno in /usr/lib:
# vanno in LD_LIBRARY_PATH. Nato in athena (nix/runtime.nix), da qui lo usano tutti.
{ pkgs }:
let
  runtime = with pkgs; [
    vulkan-loader wayland libxkbcommon fontconfig freetype
    libx11 libxcb libxcursor libxrandr libxi
  ];
  # Librerie con header per i crate -sys (pkg-config): icona di sistema (GTK), portachiavi (dbus).
  build = with pkgs; [ dbus gtk3 libayatana-appindicator ];
in
{
  inherit runtime build;
  env = {
    LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath (runtime ++ [ pkgs.stdenv.cc.cc.lib pkgs.mesa ]
      ++ [ pkgs.libayatana-appindicator pkgs.gtk3 ]);
    # Il mesa-git di sistema (/run/opengl-driver, chaotic) chiede glibc 2.43 e il sistema ha la 2.42: il suo GL
    # non si carica e senza GL Xwayland non ha DRI3, quindi Vulkan non presenta. Si usa il mesa di nixpkgs.
    __EGL_VENDOR_LIBRARY_FILENAMES = "${pkgs.mesa}/share/glvnd/egl_vendor.d/50_mesa.json";
    GBM_BACKENDS_PATH = "${pkgs.mesa}/lib/gbm";
    VK_ICD_FILENAMES = "${pkgs.mesa}/share/vulkan/icd.d/radeon_icd.x86_64.json";
  };
}
