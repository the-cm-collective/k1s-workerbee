{
  description = "k1s-workerbee local development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          linuxPackages = nixpkgs.lib.optionals pkgs.stdenv.isLinux [
            pkgs.cni-plugins
            pkgs.nerdctl
          ];
          runtimeLibs = [
            pkgs.stdenv.cc.cc.lib
            pkgs.libffi
            pkgs.openssl
            pkgs.sqlite
            pkgs.zlib
          ];
        in
        {
          default = pkgs.mkShell {
            packages =
              [
                pkgs.curl
                pkgs.git
                pkgs.imagemagick
                pkgs.jq
                pkgs.nodejs_22
                pkgs.openssl
                pkgs.python311
                pkgs.ruff
                pkgs.uv
              ]
              ++ linuxPackages;

            shellHook = ''
              export WORKERBEE_NIX_DEV=1
              export LD_LIBRARY_PATH="${nixpkgs.lib.makeLibraryPath runtimeLibs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

              mkdir -p "$PWD/.direnv/bin"
              ln -sf "${pkgs.ruff}/bin/ruff" "$PWD/.direnv/bin/ruff"
              export PATH="$PWD/.direnv/bin:$PWD/.venv/bin:$PATH"

              if [ -z "''${VIRTUAL_ENV:-}" ] && [ -d "$PWD/.venv" ]; then
                export VIRTUAL_ENV="$PWD/.venv"
              fi
            '';
          };
        }
      );
    };
}
