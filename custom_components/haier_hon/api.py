import logging
import asyncio
from pyhon import Hon

_LOGGER = logging.getLogger(__name__)

class HonApiClient:
    """Client ufficiale per comunicare con l'API Cloud hOn di Haier tramite pyhOn v0.17.5."""

    def __init__(self, username, password):
        self._username = username
        self._password = password
        self._hon = None

    async def get_devices(self):
        """Effettua il login autenticato sfruttando il contesto asincrono nativo della libreria."""
        try:
            appliances = []
            _LOGGER.info("Apertura del contesto asincrono hOn per l'utente: %s", self._username)
            
            # Utilizziamo il Context Manager asincrono rilevato dall'ispezione (__aenter__)
            # Questo garantisce che l'autenticazione e la sessione interna siano attive e valide.
            async with Hon(self._username, self._password) as hon_session:
                _LOGGER.info("Autenticazione riuscita. Esecuzione del setup dei dispositivi...")
                
                # Eseguiamo il setup dei dispositivi all'interno del contesto protetto
                await hon_session.setup()
                
                # Salviamo un riferimento all'istanza nel client se serve per i comandi successivi
                self._hon = hon_session

                # Controllo di sicurezza classico sull'esistenza dell'array degli elettrodomestici
                appliances_list = getattr(hon_session, "appliances", None)
                if not appliances_list:
                    _LOGGER.warning("Nessun dispositivo associato all'account hOn specificato.")
                    return appliances

                for appliance in appliances_list:
                    # Estraggo l'ID unico del dispositivo
                    appliance_id = appliance.info.get("applianceId") or getattr(appliance, "id", None)
                    if not appliance_id:
                        continue

                    # pyhOn v0.17.x memorizza lo stato attuale dei sensori dentro il dizionario appliance.data
                    device_raw_data = getattr(appliance, "data", {})

                    # Costruiamo la struttura attesa dai file climate.py e sensor.py dell'integrazione
                    appliance_data = {
                        "applianceId": str(appliance_id),
                        "shadow": {
                            "parameters": {
                                "onOffStatus": {"value": int(device_raw_data.get("onOffStatus", 0))},
                                "machMode": {"value": int(device_raw_data.get("machMode", 1))},
                                "tempSel": {"value": float(device_raw_data.get("tempSel", 24.0))},
                                "compressorFrequency": {"value": float(device_raw_data.get("compressorFrequency", 0.0))},
                                "tempIndoor": {"value": float(device_raw_data.get("tempIndoor", 20.0))},
                                "tempOutdoor": {"value": float(device_raw_data.get("tempOutdoor", 20.0))},
                            }
                        }
                    }
                    appliances.append(appliance_data)
                
            return appliances

        except Exception as err:
            _LOGGER.error("Errore critico definitivo di comunicazione con pyhOn: %s", err, exc_info=True)
            raise err

    async def set_device_status(self, appliance_id, parameters: dict):
        """Invia i comandi di controllo modificando le impostazioni dell'appliance sul cloud."""
        try:
            # Se dobbiamo inviare un comando, riapriamo una sessione al volo per lo scambio dati
            async with Hon(self._username, self._password) as hon_session:
                await hon_session.setup()
                for appliance in hon_session.appliances:
                    current_id = appliance.info.get("applianceId") or getattr(appliance, "id", None)
                    if str(current_id) == str(appliance_id):
                        for key, value in parameters.items():
                            if hasattr(appliance, "set_parameter"):
                                await appliance.set_parameter(key, value)
                            elif hasattr(appliance, "parameters") and key in appliance.parameters:
                                await appliance.parameters[key].set_value(value)
                        return True
            return False
        except Exception as err:
            _LOGGER.error("Impossibile inviare il comando al dispositivo %s: %s", appliance_id, err, exc_info=True)
            raise err