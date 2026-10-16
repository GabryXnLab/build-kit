{
  description = "build-kit: toolchain di build comuni (Rust, GPUI, Android, Go) per i progetti dell'organizzazione";

  # Solo nixpkgs e rust-overlay: i progetti che usano questo flake li ereditano con `follows`, così
  # c'è un solo nixpkgs nello store e un solo flake.lock da aggiornare.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, rust-overlay }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems f;
      # allowUnfree serve solo ai pacchetti dell'Android SDK (licenza da accettare); gli altri sono liberi.
      pkgsFor = system: import nixpkgs {
        inherit system;
        overlays = [ rust-overlay.overlays.default ];
        config = { allowUnfree = true; android_sdk.accept_license = true; };
      };
      mk = import ./nix/mkshell.nix;
    in
    {
      lib = {
        inherit eachSystem;
        # Libreria per i progetti: mkDevShell { system; toolchain; modules; … } (vedi nix/mkshell.nix).
        mkDevShell = args: mk (args // { pkgs = pkgsFor args.system; });
        # Librerie e variabili di runtime di GPUI, per impacchettare un binario con makeWrapper (come athena).
        gpuiRuntime = pkgs: import ./nix/gpui.nix { inherit pkgs; };
        inherit pkgsFor;
      };

      # Shell d'esempio e di prova: stable di rustup-less, tutti i moduli spenti salvo gpui.
      devShells = eachSystem (system: {
        default = mk {
          pkgs = pkgsFor system;
          toolchain = null;
          modules = [ "gpui" ];
        };
      });
    };
}
