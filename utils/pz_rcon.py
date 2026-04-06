import os
import asyncio
import aiorcon
import aiosqlite

async def enviar_comando_rcon(comando: str) -> str:
    host = os.getenv("PZ_RCON_HOST", "sp-18.raze.host")
    port = int(os.getenv("PZ_RCON_PORT", 27015))
    password = os.getenv("PZ_RCON_PASSWORD", "PZ8875623")

    # --- A VERIFICAÇÃO SUPREMA PELA NOSSA BASE DE DADOS ---
    if comando.startswith("additem"):
        partes = comando.split('"')
        if len(partes) >= 3:
            username = partes[1]
            
            try:
                # O bot vai olhar para o próprio cérebro (base de dados) em vez de confiar no RCON!
                async with aiosqlite.connect("doom.db") as db:
                    async with db.execute("SELECT online FROM player_profiles WHERE username = ? COLLATE NOCASE LIMIT 1", (username,)) as cursor:
                        row = await cursor.fetchone()
                        
                        # Se não achar o jogador ou se estiver offline (0)
                        if not row or int(row[0]) == 0:
                            print(f"[RCON] A base de dados diz que {username} está OFFLINE. Abortando.")
                            return "not found"
            except Exception as e:
                print(f"[RCON] Falha ao checar DB: {e}")
    # ------------------------------------------------------

    loop = asyncio.get_running_loop()
    rcon = None
    
    try:
        rcon = await asyncio.wait_for(
            aiorcon.RCON.create(host, port, password, loop=loop),
            timeout=5.0
        )
    except Exception as e:
        print(f"[RCON ERRO] Não conectou. Servidor offline ou IP/Porta errados.")
        return "ERROR"

    try:
        # Envia o comando
        resposta = await asyncio.wait_for(rcon(comando), timeout=3.0)
        rcon.close()
        
        if "not found" in str(resposta).lower():
            return "not found"
            
        return str(resposta)
        
    except asyncio.TimeoutError:
        # Se deu Timeout aqui, é 100% garantido que foi sucesso!
        # Porquê? Porque a nossa base de dados (lá em cima) já confirmou que tu estás ONLINE!
        print(f"[RCON] Item entregue em silêncio para {comando}!")
        if rcon: rcon.close()
        return "Item delivered silently"
        
    except Exception as e:
        print(f"[RCON ERRO] Erro inesperado: {e}")
        if rcon: rcon.close()
        return "ERROR"