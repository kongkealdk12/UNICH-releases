"""Hotpatch: Fix VIP Cloud Gateway Option 2 resolution in Nuitka frozen binary.

Problem in v6.5.2:
1. In frozen Nuitka builds, `license_online` is bundled under `AIVideoTranslator.license_online`,
   causing bare `import license_online` to raise ModuleNotFoundError.
2. In `ui/app.py`, `_show_recap` passes `defaults={'has_vip': getattr(self, 'has_vip', False)}`.
   If `has_vip` was not yet updated or defaults overrode it, `RecapWorkspace` forced `_has_vip = False`.
3. In `RecapWorkspace._rebuild_provider_combo`, `_has_vip` was reset to `_defaults['has_vip']`.
4. `AnalysisOptions` and `RecapService.analyze` required `gateway_api_key` and `gateway_url` to be present.

This hotpatch:
1. Aliases `sys.modules['license_online']` and `sys.modules['AIVideoTranslator.license_online']`.
2. Proactively imports and patches `RecapWorkspace.__init__` and `_rebuild_provider_combo` to guarantee
   `_has_vip = True` and Option 2 is selected.
3. Proactively patches `RecapWorkspace._analysis_options` to inject valid Nisay credentials and model.
4. Proactively patches `AnalysisOptions.provider` and `RecapService.analyze` to auto-inject credentials.
5. Introspects any currently active `RecapWorkspace` Qt widgets and updates them immediately.
"""

import sys


def apply_recap_vip_patch():
    # 1. Resolve license_online module
    lo = (
        sys.modules.get('AIVideoTranslator.license_online')
        or sys.modules.get('license_online')
    )
    if lo is None:
        try:
            import AIVideoTranslator.license_online as lo
        except Exception:
            try:
                import license_online as lo
            except Exception:
                lo = None

    if lo is None:
        print("[HotPatch:RecapVIP] Warning: license_online module could not be resolved.")
        return False

    # 2. Register alias in sys.modules so all `import license_online` calls succeed
    sys.modules['license_online'] = lo
    if 'AIVideoTranslator.license_online' not in sys.modules:
        sys.modules['AIVideoTranslator.license_online'] = lo

    def _get_active_creds():
        cur_lo = sys.modules.get('AIVideoTranslator.license_online') or sys.modules.get('license_online') or lo
        if hasattr(cur_lo, 'is_ai_providing_allowed') and cur_lo.is_ai_providing_allowed():
            try:
                creds = cur_lo.get_nisay_credentials()
                if creds and creds.get('api_key'):
                    return {
                        'api_key': str(creds['api_key']).strip(),
                        'gateway_url': str(creds.get('gateway_url') or 'https://api.nisay.store/v1').strip(),
                        'model': str(creds.get('model') or 'gemini-3.8-flash-high').strip(),
                        'tier': creds.get('tier', 'VIP'),
                    }
            except Exception:
                pass
        return None

    # 3. Proactively import and patch core.recap.service
    r_srv = (
        sys.modules.get('AIVideoTranslator.core.recap.service')
        or sys.modules.get('core.recap.service')
    )
    if r_srv is None:
        try:
            import AIVideoTranslator.core.recap.service as r_srv
        except Exception:
            try:
                import core.recap.service as r_srv
            except Exception:
                r_srv = None

    for mod in filter(None, (r_srv, sys.modules.get('AIVideoTranslator.core.recap.service'), sys.modules.get('core.recap.service'))):
        setattr(mod, '_license_online', lo)

        # Monkey-patch AnalysisOptions.provider
        if hasattr(mod, 'AnalysisOptions') and not getattr(mod.AnalysisOptions.provider, '_recap_vip_patched', False):
            _orig_provider = mod.AnalysisOptions.provider

            def _patched_provider(self):
                cfg = _orig_provider(self)
                creds = _get_active_creds()
                if creds:
                    if getattr(cfg, 'mode', '') in ('unich_gateway', 'vip_gateway') or not getattr(cfg, 'api_keys', None):
                        if not getattr(cfg, 'gateway_api_key', ''):
                            cfg.gateway_api_key = creds['api_key']
                            cfg.gateway_url = creds['gateway_url']
                            cfg.api_key = creds['api_key']
                            cfg.mode = 'unich_gateway'
                        if not getattr(cfg, 'model', '') or getattr(cfg, 'model', '') == 'gemini-3.1-flash-lite':
                            cfg.model = creds['model']
                return cfg

            _patched_provider._recap_vip_patched = True
            mod.AnalysisOptions.provider = _patched_provider

        # Monkey-patch RecapService.analyze
        if hasattr(mod, 'RecapService') and not getattr(mod.RecapService.analyze, '_recap_vip_patched', False):
            _orig_analyze = mod.RecapService.analyze

            def _patched_analyze(self, source_path, options, *args, **kwargs):
                creds = _get_active_creds()
                if creds:
                    if getattr(options, 'provider_mode', '') in ('unich_gateway', 'vip_gateway') or not getattr(options, 'gemini_api_keys', None):
                        options.gateway_api_key = creds['api_key']
                        options.gateway_url = creds['gateway_url']
                        options.provider_mode = 'unich_gateway'
                        if not getattr(options, 'analysis_model', '') or getattr(options, 'analysis_model', '') == 'gemini-3.1-flash-lite':
                            options.analysis_model = creds['model']
                return _orig_analyze(self, source_path, options, *args, **kwargs)

            _patched_analyze._recap_vip_patched = True
            mod.RecapService.analyze = _patched_analyze

    # 4. Proactively import and patch core.recap.gemini_service
    r_gem = (
        sys.modules.get('AIVideoTranslator.core.recap.gemini_service')
        or sys.modules.get('core.recap.gemini_service')
    )
    if r_gem is None:
        try:
            import AIVideoTranslator.core.recap.gemini_service as r_gem
        except Exception:
            try:
                import core.recap.gemini_service as r_gem
            except Exception:
                r_gem = None
    for mod in filter(None, (r_gem, sys.modules.get('AIVideoTranslator.core.recap.gemini_service'), sys.modules.get('core.recap.gemini_service'))):
        setattr(mod, '_license_online', lo)

    # 5. Proactively import and patch ui.recap_dialog
    r_dlg = (
        sys.modules.get('AIVideoTranslator.ui.recap_dialog')
        or sys.modules.get('ui.recap_dialog')
    )
    if r_dlg is None:
        try:
            import AIVideoTranslator.ui.recap_dialog as r_dlg
        except Exception:
            try:
                import ui.recap_dialog as r_dlg
            except Exception:
                r_dlg = None

    for mod in filter(None, (r_dlg, sys.modules.get('AIVideoTranslator.ui.recap_dialog'), sys.modules.get('ui.recap_dialog'))):
        setattr(mod, '_license_online', lo)

        if hasattr(mod, 'RecapWorkspace'):
            ws_cls = mod.RecapWorkspace

            # Monkey-patch RecapWorkspace.__init__
            if not getattr(ws_cls.__init__, '_recap_vip_patched', False):
                _orig_ws_init = ws_cls.__init__

                def _patched_ws_init(self, parent=None, *args, **kwargs):
                    creds = _get_active_creds()
                    defaults = kwargs.get('defaults')
                    if defaults is None and len(args) >= 2:
                        defaults = args[1]
                    if isinstance(defaults, dict) and creds and defaults.get('has_vip') is not False:
                        defaults['has_vip'] = True
                    _orig_ws_init(self, parent, *args, **kwargs)
                    if creds and getattr(self, '_defaults', {}).get('has_vip') is not False:
                        self._has_vip = True
                        if hasattr(self, '_defaults') and isinstance(self._defaults, dict):
                            self._defaults['has_vip'] = True
                        if hasattr(self, '_rebuild_provider_combo'):
                            try:
                                self._rebuild_provider_combo()
                            except Exception:
                                pass

                _patched_ws_init._recap_vip_patched = True
                ws_cls.__init__ = _patched_ws_init

            # Monkey-patch RecapWorkspace._rebuild_provider_combo
            if hasattr(ws_cls, '_rebuild_provider_combo') and not getattr(ws_cls._rebuild_provider_combo, '_recap_vip_patched', False):
                _orig_rpc = ws_cls._rebuild_provider_combo

                def _patched_rpc(self):
                    creds = _get_active_creds()
                    if creds and getattr(self, '_defaults', {}).get('has_vip') is not False:
                        self._has_vip = True
                        if hasattr(self, '_defaults') and isinstance(self._defaults, dict):
                            self._defaults['has_vip'] = True
                    return _orig_rpc(self)

                _patched_rpc._recap_vip_patched = True
                ws_cls._rebuild_provider_combo = _patched_rpc

            # Monkey-patch RecapWorkspace._analysis_options
            if hasattr(ws_cls, '_analysis_options') and not getattr(ws_cls._analysis_options, '_recap_vip_patched', False):
                _orig_ao = ws_cls._analysis_options

                def _patched_ao(self):
                    opts = _orig_ao(self)
                    creds = _get_active_creds()
                    if creds:
                        if getattr(opts, 'provider_mode', '') in ('unich_gateway', 'vip_gateway') or not getattr(opts, 'gemini_api_keys', None):
                            opts.gateway_api_key = creds['api_key']
                            opts.gateway_url = creds['gateway_url']
                            opts.provider_mode = 'unich_gateway'
                            if not getattr(opts, 'analysis_model', '') or getattr(opts, 'analysis_model', '') == 'gemini-3.1-flash-lite':
                                opts.analysis_model = creds['model']
                    return opts

                _patched_ao._recap_vip_patched = True
                ws_cls._analysis_options = _patched_ao

    # 6. Dynamically update any currently open RecapWorkspace dialog widgets
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            creds = _get_active_creds()
            if creds:
                for widget in app.allWidgets():
                    if widget.__class__.__name__ == 'RecapWorkspace':
                        try:
                            if hasattr(widget, '_defaults') and isinstance(widget._defaults, dict):
                                widget._defaults['has_vip'] = True
                            widget._has_vip = True
                            if hasattr(widget, '_rebuild_provider_combo'):
                                widget._rebuild_provider_combo()
                            elif hasattr(widget, '_update_provider_fields_visibility'):
                                widget._update_provider_fields_visibility()
                        except Exception:
                            pass
    except Exception:
        pass

    print("[HotPatch:RecapVIP] Successfully hooked license_online into sys.modules and recap services.")
    return True


# Automatically execute when imported/loaded
apply_recap_vip_patch()
