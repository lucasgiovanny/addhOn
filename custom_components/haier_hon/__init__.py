import logging
from datetime import timedelta
import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PLATFORMS, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura l'integrazione Haier hOn partendo da un Config Entry."""
    from .hon_client import HonClient

    # FIX: la chiave salvata dal config_flow è "email", non "username"
    email = entry.data.get("email")
    password = entry.data.get("password")

    if not email:
        _LOGGER.error(
            "Credenziali mancanti nel config entry (chiave 'email' assente). "
            "Rimuovi e riconfigura l'integrazione."
        )
        return False

    hon_client = HonClient(email=email, password=password)

    # Setup iniziale di pyhOn in executor (non blocca l'event loop di HA)
    try:
        await hass.async_add_executor_job(hon_client.setup_sync)
    except Exception as err:
        _LOGGER.error("Impossibile connettersi a hOn: %s", err)
        return False

    async def async_update_data() -> dict:
        """Recupera i dati aggiornati da tutti i dispositivi hOn."""
        try:
            return await hon_client.async_get_appliances_data()
        except Exception as err:
            raise UpdateFailed(f"Errore aggiornamento hOn: {err}") from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Haier hOn data",
        update_method=async_update_data,
        update_interval=timedelta(seconds=SCAN_INTERVAL),
    )

    # Primo fetch
    await coordinator.async_config_entry_first_refresh()

    # FIX: salva sia il coordinator che il client nella struttura attesa da tutte le piattaforme
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "client": hon_client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scarica il config entry quando l'integrazione viene disattivata."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        client = entry_data.get("client")
        if client is not None:
            try:
                await client.async_close()
            except Exception as err:
                _LOGGER.warning("Errore chiusura HonClient: %s", err)
    return unload_ok
