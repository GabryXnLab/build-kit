# mkDevShell: devShell di un progetto dal suo rust-toolchain.toml più moduli opzionali.
#
#   build-kit.lib.mkDevShell {
#     system = "x86_64-linux";
#     toolchain = ./rust-toolchain.toml;      # null: rust stable di rust-overlay
#     modules = [ "gpui" "android" "go" ];    # tutti facoltativi
#     android = { sdk = null; buildTools = [ "36.0.0" ]; ndk = [ "27.3.13750724" ]; };   # facoltativo
#     extraPackages = [ ];                    # facoltativo
#   }
{ pkgs, system ? null, toolchain ? null, modules ? [ ], android ? { }, extraPackages ? [ ], go ? pkgs.go }:
let
  has = m: builtins.elem m modules;
  rust =
    if toolchain == null then pkgs.rust-bin.stable.latest.default
    else pkgs.rust-bin.fromRustupToolchainFile toolchain;
  gpui = import ./gpui.nix { inherit pkgs; };
  droid = import ./android.nix ({ inherit pkgs; } // android);
  # Gli header di sistema per i crate -sys (pkg-config) e libclang per bindgen.
  base = [ rust pkgs.rust-analyzer pkgs.pkg-config pkgs.cmake pkgs.perl pkgs.clang ]
    # Strumenti che i test si aspettano di trovare (su NixOS non ci sono in /usr/bin): openssl, tmux, procps, which.
    ++ [ pkgs.openssl pkgs.tmux pkgs.procps pkgs.which ];
in
pkgs.mkShell ({
  packages = base ++ extraPackages
    ++ pkgs.lib.optionals (has "gpui") (gpui.runtime ++ gpui.build)
    ++ pkgs.lib.optionals (has "android") droid.packages
    ++ pkgs.lib.optionals (has "go") [ go ];
  LIBCLANG_PATH = "${pkgs.libclang.lib}/lib";
}
// pkgs.lib.optionalAttrs (has "gpui") gpui.env
// pkgs.lib.optionalAttrs (has "android") droid.env)
