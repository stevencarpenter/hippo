{
  description = "hippo — local knowledge capture daemon (Rust daemon only; the Python brain stays on uv)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
  };

  outputs =
    { self, nixpkgs }:
    let
      # Darwin only: the daemon links macOS frameworks and installs LaunchAgents.
      # Adding Linux means porting install.rs, not just widening this list.
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Scoped to what the crate build reads. Excludes brain/, extension/, docs/
      # and otel/ so a Python or docs edit does not invalidate the Rust build.
      #
      # config/ and launchd/ are NOT optional: main.rs `include_str!`s the
      # default config and the LaunchAgent plist templates from outside the
      # crate directory, so omitting them fails the build, not just runtime.
      src =
        let
          keep = [
            "Cargo.toml"
            "Cargo.lock"
            "crates"
            "config"
            "launchd"
          ];
        in
        nixpkgs.lib.cleanSourceWith {
          src = ./.;
          filter =
            path: _type:
            let
              rel = nixpkgs.lib.removePrefix (toString ./. + "/") (toString path);
              head = builtins.head (nixpkgs.lib.splitString "/" rel);
            in
            builtins.elem head keep;
        };

      version = (builtins.fromTOML (builtins.readFile ./Cargo.toml)).workspace.package.version;
    in
    {
      packages = forAllSystems (pkgs: rec {
        default = hippo-daemon;

        # The `hippo` binary: daemon, CLI, and `hippo doctor`.
        #
        # Default features are OFF to match the published release artifact,
        # which builds `--no-default-features` to keep the binary minimal. The
        # crate's own default enables `otel`; use the `hippo-daemon-otel`
        # package below when you want the instrumented build.
        hippo-daemon = pkgs.rustPlatform.buildRustPackage {
          pname = "hippo-daemon";
          inherit version src;

          cargoLock.lockFile = ./Cargo.lock;

          cargoBuildFlags = [
            "-p"
            "hippo-daemon"
            "--bin"
            "hippo"
          ];
          buildNoDefaultFeatures = true;

          # build.rs derives the version from `git describe`, which cannot run
          # in the nix sandbox. State it instead, matching what a build from a
          # release tag produces, so `hippo doctor` sees the CLI, daemon, and
          # brain agree. A build from an untagged commit therefore reports the
          # workspace version rather than a commit-distance string.
          HIPPO_VERSION_FULL = version;

          # rusqlite is vendored with the `bundled` feature, so SQLite is
          # compiled from source here rather than linked from nixpkgs.
          nativeBuildInputs = [ pkgs.pkg-config ];
          buildInputs = [
            pkgs.libiconv
            # reqwest uses native-tls, which is Security.framework on darwin.
            pkgs.apple-sdk
          ];

          # The workspace's tests reach for a real SQLite database, launchd, and
          # in some suites the network. `cargo test` belongs in CI, not in the
          # sandboxed nix build.
          doCheck = false;

          meta = {
            description = "Local knowledge capture daemon for macOS";
            homepage = "https://github.com/stevencarpenter/hippo";
            mainProgram = "hippo";
            platforms = systems;
          };
        };

        hippo-daemon-otel = hippo-daemon.overrideAttrs (old: {
          pname = "${old.pname}-otel";
          buildNoDefaultFeatures = false;
        });
      });

      # `nix run github:stevencarpenter/hippo`
      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.hippo-daemon}/bin/hippo";
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
