{
  description = "Full-text terminal search for Claude, Codex and OpenCode chats";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # Without this devshell drags in (and evaluates against) its own nixpkgs
    # pin -- the upstream `nix flake init -t github:numtide/devshell` template
    # leaves it out.
    devshell.url = "github:numtide/devshell";
    devshell.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, flake-utils, devshell }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ devshell.overlays.default ];
        };
      in
      {
        packages.default = pkgs.stdenvNoCC.mkDerivation {
          pname = "chat-index";
          version = "0.1.0";
          src = ./.;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          doCheck = true;
          checkPhase = ''
            ${pkgs.python3}/bin/python3 -m unittest discover -s tests -v
          '';
          installPhase = ''
            mkdir -p $out/lib $out/bin
            cp chat_index.py $out/lib/chat_index.py
            makeWrapper ${pkgs.python3}/bin/python3 $out/bin/chat-index \
              --add-flags "$out/lib/chat_index.py"
          '';
        };
        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/chat-index";
        };
        devShells.default = pkgs.devshell.mkShell {
          imports = [ (pkgs.devshell.importTOML ./devshell.toml) ];
        };
      });
}
