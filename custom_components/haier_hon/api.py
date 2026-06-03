import logging
import asyncio
from pyhon import Hon

_LOGGER = logging.getLogger(__name__)

class HonApiClient:
    """Client per comunicare con l'API Cloud hOn di Haier tramite pyhOn."""

    def __init__(self, username, password):
        self._username = username
        self._password = password
        self._hon = None

    async def get_devices(self):
        """Esegue il login ed estrae i dispositivi usando la sintassi nativa di pyhOn v0.17.5."""
        try:
            if self._hon is None:
                _LOGGER.info("Inizializzazione della sessione pyhOn per %s...", self._username)
                self._hon = Hon(self._username, self._password)
                await self._hon.setup()

            appliances = []
            
            if not self._hon.appliances:
                _LOGGER.warning("Nessun dispositivo trovato sull'account hOn.")
                return appliances

            for appliance in self._hon.appliances:
                # pyhOn memorizza l'ID in appliance.info.get("applianceId") o appliance.id
                appliance_id = appliance.info.get("applianceId") or getattr(appliance, "id", None)
                if not appliance_id:
                    continue
                
                # Funzione di supporto interna per estrarre i dati in base a come pyhOn espone lo stato
                def get_param_value(key, default=0):
                    try:
                        # 1. Prova a prenderlo dai parametri attuali di pyhOn
                        if hasattr(appliance, "parameters") and key in appliance.parameters:
                            return appliance.parameters[key].value
                        # 2. Prova a prenderlo dal dizionario delle proprietà/stato
                        if hasattr(appliance, "properties") and key in appliance.properties:
                            return appliance.properties[key].get("value", default)
                        if hasattr(appliance, "status") and key in appliance.status:
                            return appliance.status[key]
                        # 3. Ripiego provando a leggere l'attributo direttamente se esistente
                        return getattr(appliance, key, default)
                    except Exception:
                        return default

                # Costruiamo la struttura attesa da climate.py e sensor.py
                appliance_data = {
                    "applianceId": str(appliance_id),
                    "shadow": {
                        "parameters": {
                            "onOffStatus": {"value": int(get_param_value("onOffStatus", 0))},
                            "machMode": {"value": int(get_param_value("machMode", 1))},
                            "tempSel": {"value": float(get_param_value("tempSel", 24))},
                            "compressorFrequency": {"value": float(get_param_value("compressorFrequency", 0))},
                            "tempIndoor": {"value": float(get_param_value("tempIndoor", 20))},
                            "tempOutdoor": {"value": float(get_param_value("tempOutdoor", 20))},
                        }
                    }
                }
                appliances.append(appliance_data)
                
            return appliances
        except Exception as err:
            _LOGGER.error("Errore critico durante il get_devices nel client API: %s", err)
            raise err

    async def set_device_status(self, appliance_id, parameters: dict):
        """Invia i comandi impostando i parametri direttamente sull'oggetto pyhOn."""
        try:
            if self._hon is None:
                return False
                
            for appliance in self._hon.appliances:
                current_id = appliance.info.get("applianceId") or getattr(appliance, "id", None)
                if str(current_id) == str(appliance_id):
                    for key, value in parameters.items():
                        if hasattr(appliance, "parameters") and key in appliance.parameters:
                            await appliance.parameters[key].set_value(value)
                        elif hasattr(appliance, "set_parameter"):
                            await appliance.set_parameter(key, value)
                    return True
            return False
        except Exception as err:
            _LOGGER.error("Impossibile inviare il comando al dispositivo %s: %s", appliance_id, err)
            raise err