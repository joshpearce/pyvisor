{
  description = "Pyvisor's pinned offline Python sandbox rootfs";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = false;
      };
      pythonPackages = pkgs.python313Packages;
      sandboxPython = pkgs.python313.withPackages (ps: [
        ps.numpy
        ps.pandas
        ps.scipy
        ps.scikit-learn
        ps.matplotlib
        ps.sympy
        ps.networkx
        ps.statsmodels
        ps.pip
      ]);
      rootfsEnvironment = pkgs.buildEnv {
        name = "pyvisor-sandbox-environment";
        paths = [
          pkgs.bash
          pkgs.coreutils
          pkgs.diffutils
          pkgs.file
          pkgs.findutils
          pkgs.gawk
          pkgs.gnugrep
          pkgs.gnused
          pkgs.less
          pkgs.which
          sandboxPython
        ];
        pathsToLink = [ "/bin" ];
      };
      rootfsClosure = pkgs.closureInfo {
        rootPaths = [ rootfsEnvironment ];
      };
      imageManifest = pkgs.writeText "pyvisor-sandbox-image.json" (builtins.toJSON {
        format_version = 1;
        python = {
          implementation = "CPython";
          version = pkgs.python313.version;
        };
        packages = {
          numpy = pythonPackages.numpy.version;
          pandas = pythonPackages.pandas.version;
          scipy = pythonPackages.scipy.version;
          scikit_learn = pythonPackages.scikit-learn.version;
          matplotlib = pythonPackages.matplotlib.version;
          sympy = pythonPackages.sympy.version;
          networkx = pythonPackages.networkx.version;
          statsmodels = pythonPackages.statsmodels.version;
          pip = pythonPackages.pip.version;
        };
        shell_utilities = [
          "bash"
          "coreutils"
          "diffutils"
          "file"
          "findutils"
          "gawk"
          "gnugrep"
          "gnused"
          "less"
          "which"
        ];
      });
      sandboxRootfs = pkgs.runCommand "pyvisor-sandbox-rootfs-${pkgs.python313.version}" { } ''
        mkdir -p "$out/bin" "$out/etc" "$out/nix/store" "$out/tmp" "$out/usr/local/bin" "$out/workspace"
        chmod 1777 "$out/tmp"

        # Nix executables and shared libraries use absolute /nix/store paths.
        # Copy the complete closure into the rootfs so those references remain
        # valid after runsc changes the process root.
        while IFS= read -r store_path; do
          case "$store_path" in
            /nix/store/*) cp -a "$store_path" "$out$store_path" ;;
            *) echo "unexpected closure path: $store_path" >&2; exit 1 ;;
          esac
        done < ${rootfsClosure}/store-paths

        cp -a ${rootfsEnvironment}/bin/. "$out/bin/"
        # ``cp -a`` preserves the source directory's store-mode metadata,
        # including its lack of write permission.  Restore write access before
        # replacing the environment-provided ``sh`` link below.
        chmod u+w "$out/bin"
        ln -s /bin "$out/usr/bin"
        ln -s /bin/python3 "$out/usr/local/bin/python3"
        ln -sfn bash "$out/bin/sh"
        install -Dm0444 ${imageManifest} "$out/etc/pyvisor-sandbox-image.json"
      '';
    in
    {
      packages.${system} = {
        sandbox-rootfs = sandboxRootfs;
        default = sandboxRootfs;
      };
    };
}
