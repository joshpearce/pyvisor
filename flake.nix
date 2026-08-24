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
      pyvisorApp = pythonPackages.buildPythonApplication {
        pname = "pyvisor";
        version = "0.1.0";
        pyproject = true;
        src = ./.;
        build-system = [ pythonPackages.hatchling ];
        dependencies = [
          pythonPackages.fastapi
          pythonPackages.pydantic
          pythonPackages.uvicorn
        ];
        # The uv lock retains the exact development versions. This flake pins
        # Nixpkgs separately, so relax only wheel metadata before building
        # against that pinned Python package set.
        postPatch = ''
          substituteInPlace pyproject.toml \
            --replace-fail "hatchling==1.27.0" "hatchling" \
            --replace-fail "fastapi==0.115.12" "fastapi" \
            --replace-fail "pydantic==2.10.6" "pydantic" \
            --replace-fail "uvicorn==0.34.0" "uvicorn"
        '';
        doCheck = false;
      };
      pyvisorContainer = pkgs.dockerTools.buildLayeredImage {
        name = "localhost/pyvisor";
        tag = "0.1.0";
        contents = [
          pyvisorApp
          pkgs.curl
          pkgs.gvisor
          sandboxRootfs
        ];
        config = {
          Cmd = [ "${pyvisorApp}/bin/pyvisor" ];
          Env = [
            "PYTHONUNBUFFERED=1"
            "PYVISOR_EXECUTION_BACKEND=runsc"
            "PYVISOR_LISTEN_HOST=0.0.0.0"
            "PYVISOR_RUNSC_BINARY=${pkgs.gvisor}/bin/runsc"
            "PYVISOR_SANDBOX_ROOTFS=${sandboxRootfs}"
            "PYVISOR_SANDBOX_IMAGE_IDENTITY=pyvisor-sandbox-python-3.13@${builtins.baseNameOf sandboxRootfs}"
            "PYVISOR_SESSIONS_ROOT=/var/lib/pyvisor/sessions"
            "PYVISOR_RUNTIME_ROOT=/var/lib/pyvisor/runtime"
          ];
          ExposedPorts = { "8000/tcp" = { }; };
        };
      };
    in
    {
      packages.${system} = {
        sandbox-rootfs = sandboxRootfs;
        pyvisor = pyvisorApp;
        pyvisor-container = pyvisorContainer;
        default = sandboxRootfs;
      };
    };
}
