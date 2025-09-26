# main.py - Versão com Acumulador e Cooldown de Estresse

import cv2
from deepface import DeepFace
import time
from plyer import notification
import platform
import os
import ctypes
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
import numpy as np
from collections import Counter, deque
import math

# --- CONFIGURAÇÕES GERAIS ---
MODO_APRESENTACAO = True
HISTORICO_EMO_TAMANHO = 30
EMOCOES_IGNORADAS = set()

# --- CONFIGURAÇÕES DE ESTRESSE ---
EMOCOES_ALVO_ESTRESSE = {'angry', 'sad', 'fear'}
LIMITE_AGITACAO = 90
MOVIMENTO_BRUSCO_THRESHOLD = 7
PESO_EMOCAO_DETECTADA = 10
PESO_NEUTRO_DECAIMENTO = 1
ESTRESSE_COOLDOWN = 10  # Segundos que o estresse "congelado" persiste

# --- CONFIGURAÇÕES DOS TIMERS ---
if MODO_APRESENTACAO:
    JANELA_DE_AVISO, LIMITE_AUSENCIA, LIMITE_SUSPENSAO = 5, 15, 35
    LIMITE_ESTRESSE, INTERVALO_DESCANSO_OLHOS, TEMPO_PARA_FOCO = 4, 25, 10
else:
    JANELA_DE_AVISO, LIMITE_AUSENCIA, LIMITE_SUSPENSAO = 15, 45, 5 * 60
    LIMITE_ESTRESSE, INTERVALO_DESCANSO_OLHOS, TEMPO_PARA_FOCO = 10, 20 * 60, 5 * 60


# --- FUNÇÕES DE AÇÃO ---
def bloquear_tela():
    sistema = platform.system()
    if sistema == "Windows": ctypes.windll.user32.LockWorkStation()


def suspender_pc():
    sistema = platform.system()
    if sistema == "Windows": os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")


def get_system_sounds_session():
    sessions = AudioUtilities.GetAllSessions()
    for session in sessions:
        if session.Process is None: return session
    return None


def ativar_modo_foco():
    print("ATIVANDO MODO FOCO...")
    try:
        session = get_system_sounds_session()
        if session: session.SimpleAudioVolume.SetMute(1, None)
    except Exception:
        pass


def desativar_modo_foco():
    print("DESATIVANDO MODO FOCO...")
    try:
        session = get_system_sounds_session()
        if session: session.SimpleAudioVolume.SetMute(0, None)
    except Exception:
        pass


def desenhar_painel_diagnostico(frame_camera, dados):
    h_cam, w_cam, _ = frame_camera.shape
    painel_h = 220
    painel = np.zeros((painel_h, w_cam, 3), dtype="uint8")
    BRANCO, VERDE, AMARELO, VERMELHO, AZUL_CLARO, LARANJA = (255, 255, 255), (100, 255, 100), (100, 255, 255), (100,
                                                                                                                100,
                                                                                                                255), (
        255, 255, 100), (0, 165, 255)
    cv2.putText(painel, "PAINEL DE DIAGNOSTICO - IRIS", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, BRANCO, 2)
    status_cor = VERDE if dados['status'] == "PRESENTE" else VERMELHO
    cv2.putText(painel, f"Status: {dados['status']}", (10, 60), cv2.FONT_HERSHEY_PLAIN, 1.5, status_cor, 2)
    cv2.putText(painel, f"Emocao: {dados['emocao']}", (w_cam - 350, 60), cv2.FONT_HERSHEY_PLAIN, 1.5, VERDE, 2)

    def desenhar_barra(y, texto, percent, cor):
        cv2.putText(painel, texto, (10, y + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, BRANCO, 1)
        cv2.rectangle(painel, (180, y), (w_cam - 10, y + 20), (255, 255, 255), 1)
        bar_w = int((w_cam - 192) * percent)
        if bar_w > 0: cv2.rectangle(painel, (181, y + 1), (180 + bar_w, y + 19), cor, -1)

    desenhar_barra(90, "Foco", dados['foco_percent'], AZUL_CLARO)
    desenhar_barra(120, "Estresse Emo.", dados['estresse_percent'], AMARELO)
    desenhar_barra(150, "Agitacao Fisica", dados['agitacao_percent'], LARANJA)
    desenhar_barra(180, "Ausencia", dados['ausencia_percent'], VERMELHO)
    return np.vstack((frame_camera, painel))


# --- INICIALIZAÇÃO ---
cap = cv2.VideoCapture(0)
if not cap.isOpened(): exit()
historico_emocoes = deque(maxlen=HISTORICO_EMO_TAMANHO)
emocao_estavel = "N/A"
ultimo_rosto_visto = time.time()
tela_bloqueada, suspensao_ativada = False, False
aviso_bloqueio_enviado, aviso_suspensao_enviado = False, False
notificacao_estresse_enviada = False
inicio_tempo_tela, inicio_foco, modo_foco_ativo = None, None, False
ultima_posicao_rosto, pontuacao_agitacao = None, 0
tempo_estresse_acumulado = 0.0
cooldown_estresse_timer = None
ultimo_frame_time = time.time()

# --- LOOP PRINCIPAL ---
while True:
    agora = time.time()
    dt = agora - ultimo_frame_time
    ultimo_frame_time = agora

    ret, frame = cap.read()
    if not ret: break
    frame_espelhado = cv2.flip(frame, 1)
    rosto_detectado_neste_frame = False
    resultado_rosto = None

    try:
        resultado_rosto = DeepFace.analyze(frame_espelhado, actions=['emotion'], enforce_detection=False, silent=True)
        if resultado_rosto and len(resultado_rosto) > 0 and resultado_rosto[0]['face_confidence'] > 0.8:
            rosto_detectado_neste_frame = True
            emocao_detectada = resultado_rosto[0]['dominant_emotion']
            if emocao_detectada == 'neutral':
                historico_emocoes.append('neutral')
            elif emocao_detectada not in EMOCOES_IGNORADAS:
                for _ in range(PESO_EMOCAO_DETECTADA):
                    historico_emocoes.append(emocao_detectada)
            for _ in range(PESO_NEUTRO_DECAIMENTO):
                historico_emocoes.append('neutral')
            if len(historico_emocoes) > 10:
                emocao_estavel = Counter(historico_emocoes).most_common(1)[0][0]
            else:
                emocao_estavel = "CALIBRANDO..."
            regiao = resultado_rosto[0]['region']
            rosto_x, rosto_y = regiao['x'] + regiao['w'] // 2, regiao['y'] + regiao['h'] // 2
            if ultima_posicao_rosto is not None:
                distancia = math.sqrt(
                    (rosto_x - ultima_posicao_rosto[0]) ** 2 + (rosto_y - ultima_posicao_rosto[1]) ** 2)
                if distancia > MOVIMENTO_BRUSCO_THRESHOLD:
                    pontuacao_agitacao = min(LIMITE_AGITACAO, pontuacao_agitacao + 25)
                else:
                    pontuacao_agitacao = max(0, pontuacao_agitacao - 1)
            ultima_posicao_rosto = (rosto_x, rosto_y)
    except Exception:
        historico_emocoes.clear()
        emocao_estavel = "N/A"
        ultima_posicao_rosto = None
        pontuacao_agitacao = max(0, pontuacao_agitacao - 2)

    if rosto_detectado_neste_frame:
        ultimo_rosto_visto = time.time()
        if any([tela_bloqueada, suspensao_ativada, aviso_bloqueio_enviado, aviso_suspensao_enviado]):
            tela_bloqueada, suspensao_ativada, aviso_bloqueio_enviado, aviso_suspensao_enviado = False, False, False, False
        if inicio_tempo_tela is None: inicio_tempo_tela = time.time()
        if (agora - inicio_tempo_tela) > INTERVALO_DESCANSO_OLHOS:
            notification.notify(title='IRIS - Hora de Cuidar dos Olhos!',
                                message='Pausa de 20s para olhar para um objeto distante.', app_name='IRIS')
            inicio_tempo_tela = agora

        emocao_de_estresse_detectada = emocao_estavel in EMOCOES_ALVO_ESTRESSE
        if emocao_de_estresse_detectada:
            tempo_estresse_acumulado += dt
            cooldown_estresse_timer = None
        else:
            if tempo_estresse_acumulado > 0:
                if cooldown_estresse_timer is None:
                    cooldown_estresse_timer = agora
                if (agora - cooldown_estresse_timer) > ESTRESSE_COOLDOWN:
                    tempo_estresse_acumulado = 0
                    cooldown_estresse_timer = None

        trigger_emocional = tempo_estresse_acumulado >= LIMITE_ESTRESSE
        trigger_fisico = pontuacao_agitacao >= LIMITE_AGITACAO
        if (trigger_emocional or trigger_fisico) and not notificacao_estresse_enviada:
            motivo = "Emocional" if trigger_emocional else "Fisico (Agitacao)"
            notification.notify(title='IRIS - Hora de uma Pausa',
                                message=f'Sinal de estresse detectado ({motivo}). Respire fundo.', app_name='IRIS')
            notificacao_estresse_enviada = True
            pontuacao_agitacao = 0
        elif not trigger_emocional and not trigger_fisico:
            notificacao_estresse_enviada = False

        em_estado_de_estresse = tempo_estresse_acumulado > 0 or trigger_fisico
        condicao_foco = not em_estado_de_estresse
        if condicao_foco:
            if inicio_foco is None: inicio_foco = time.time()
            if (agora - inicio_foco > TEMPO_PARA_FOCO) and not modo_foco_ativo:
                ativar_modo_foco()
                modo_foco_ativo = True
        else:
            inicio_foco = None
            if modo_foco_ativo:
                desativar_modo_foco()
                modo_foco_ativo = False
    else:
        tempo_sem_rosto = agora - ultimo_rosto_visto
        historico_emocoes.clear()
        emocao_estavel = "N/A"
        ultima_posicao_rosto = None
        pontuacao_agitacao = max(0, pontuacao_agitacao - 2)
        tempo_estresse_acumulado = 0
        cooldown_estresse_timer = None
        if tempo_sem_rosto > LIMITE_SUSPENSAO and not suspensao_ativada:
            suspender_pc()
            suspensao_ativada, tela_bloqueada = True, True
        elif tempo_sem_rosto > (LIMITE_SUSPENSAO - JANELA_DE_AVISO) and not aviso_suspensao_enviado:
            notification.notify(title='IRIS - Aviso de Suspensão', message=f'O PC será suspenso em {JANELA_DE_AVISO}s.',
                                app_name='IRIS')
            aviso_suspensao_enviado = True
        elif tempo_sem_rosto > LIMITE_AUSENCIA and not tela_bloqueada:
            bloquear_tela()
            tela_bloqueada = True
        elif tempo_sem_rosto > (LIMITE_AUSENCIA - JANELA_DE_AVISO) and not aviso_bloqueio_enviado:
            notification.notify(title='IRIS - Aviso de Bloqueio',
                                message=f'A tela será bloqueada em {JANELA_DE_AVISO}s.', app_name='IRIS')
            aviso_bloqueio_enviado = True
        inicio_tempo_tela, inicio_foco = None, None
        if modo_foco_ativo:
            desativar_modo_foco()
            modo_foco_ativo = False

    dados_diagnostico = {
        "status": "PRESENTE" if rosto_detectado_neste_frame else "AUSENTE",
        "emocao": emocao_estavel.upper(),
        "foco_percent": np.clip((agora - inicio_foco) / TEMPO_PARA_FOCO if inicio_foco else 0, 0, 1),
        "estresse_percent": np.clip(tempo_estresse_acumulado / LIMITE_ESTRESSE, 0, 1),
        "ausencia_percent": np.clip(
            (agora - ultimo_rosto_visto) / LIMITE_AUSENCIA if not rosto_detectado_neste_frame else 0, 0, 1),
        "foco_ativo": modo_foco_ativo,
        "agitacao_percent": np.clip(pontuacao_agitacao / LIMITE_AGITACAO, 0, 1)
    }
    if rosto_detectado_neste_frame and resultado_rosto:
        regiao = resultado_rosto[0]['region']
        x, y, w, h = regiao['x'], regiao['y'], regiao['w'], regiao['h']
        cv2.rectangle(frame_espelhado, (x, y), (x + w, y + h), (100, 255, 100), 2)

    frame_com_painel = desenhar_painel_diagnostico(frame_espelhado, dados_diagnostico)
    cv2.imshow('IRIS - Cerebro (Modo Apresentacao)', frame_com_painel)

    if cv2.waitKey(1) & 0xFF == ord('q'): break

if modo_foco_ativo: desativar_modo_foco()
cap.release()
cv2.destroyAllWindows()
print("Aplicação IRIS finalizada.")