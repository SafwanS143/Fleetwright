/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "bme280.h"   /* vendored BME280 driver: NVM calib + Bosch compensation */
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
/* The "MPU-6050" GY-521 breakout is actually populated with an MPU-6500
   (WHO_AM_I reads 0x70, not the 6050's 0x68) - a common substitution on
   cheap modules. The MPU-6500 is register-map compatible for everything we
   do: same 7-bit address (0x68), same PWR_MGMT_1 sleep bit, and identical
   accel/gyro sensitivity scale factors, so the sampling/conversion code is
   unchanged. (The two parts' on-die temperature formulas differ, but that
   doesn't affect us - telemetry temperature comes from the BME280, not the
   IMU.) */
#define MPU6500_I2C_ADDR    (0x68 << 1)  /* 7-bit 0x68, HAL wants it << 1 */
#define MPU6500_REG_WHOAMI  0x75U
#define MPU6500_WHOAMI_ID   0x70U        /* MPU-6500 (an MPU-6050 reads 0x68) */
#define MPU6500_I2C_TIMEOUT 100U         /* ms */
#define MPU6500_REG_PWR_MGMT_1   0x6BU   /* write 0x00 to clear SLEEP on boot */
#define MPU6500_REG_ACCEL_XOUT_H 0x3BU   /* first of the 14 burst data bytes */
#define MPU6500_BURST_LEN        14U     /* accel(6) + temp(2) + gyro(6) */
#define MPU6500_ACCEL_SENS       16384.0f/* LSB per g,   default +/-2 g   range */
#define MPU6500_GYRO_SENS        131.0f  /* LSB per dps, default +/-250 dps range */
/* On-chip Digital Low-Pass Filter config. Left at reset defaults the gyro runs
   at ~250 Hz bandwidth and passes all its broadband noise straight through; at
   our 10 Hz telemetry rate we want none of that. Writing CONFIG selects a
   ~41 Hz gyro cutoff and ACCEL_CONFIG2 a ~44 Hz accel cutoff. (DLPF is enabled
   only while FCHOICE_B = 0, which is the reset state of GYRO_CONFIG, so we don't
   touch that register.) */
#define MPU6500_REG_CONFIG        0x1AU  /* gyro/temp DLPF_CFG in bits [2:0] */
#define MPU6500_REG_ACCEL_CONFIG2 0x1DU  /* accel A_DLPF_CFG in bits [2:0] */
#define MPU6500_DLPF_CFG_41HZ     0x03U  /* gyro  DLPF ~41 Hz */
#define MPU6500_ADLPF_CFG_44HZ    0x03U  /* accel DLPF ~44 Hz */
/* Gyro zero-rate calibration: samples averaged at boot while the board is still. */
#define MPU6500_GYRO_CAL_SAMPLES  200U
/* Sanity bound on the computed bias: a real zero-rate offset is only a few dps,
   so anything past ~20 dps of raw counts means the average was contaminated
   (motion, or a start-up transient railing an axis). Reject it and leave that
   axis uncorrected rather than baking in garbage. 20 dps * 131 LSB/dps ~= 2620. */
#define MPU6500_GYRO_BIAS_MAX_CNT 2620

/* BME280 chip-ID sanity check: genuine BME280 reports 0x60 in its id register.
   A BMP280 reports 0x58 and has NO humidity sensor, so it would silently break
   the humidity telemetry field - verify the part before trusting it. */
#define BME280_I2C_ADDR     (0x76 << 1)  /* 7-bit 0x76, HAL wants it << 1 */
#define BME280_REG_ID       0xD0U
#define BME280_CHIP_ID      0x60U        /* BME280 (a BMP280 reads 0x58) */
#define BME280_I2C_TIMEOUT  100U         /* ms */

/* --- Sampling + telemetry (Chunks 7-8) -------------------------------------*/
#define SAMPLE_PERIOD_MS       100U   /* fixed 10 Hz sampling cadence */
#define HEARTBEAT_LED_MS       500U   /* LD2 blink half-period, independent of sampling */
#define HEARTBEAT_MSG_MS       5000U  /* "I'm alive" telemetry line interval (liveness) */
#define TELEMETRY_UART_TIMEOUT 100U   /* ms to push one JSON line over UART */
#define TELEMETRY_LINE_MAX     200U   /* max bytes in one JSON telemetry line */
#define DEVICE_ID              "fleet-edge-01"   /* this node's stable fleet identity */
/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
I2C_HandleTypeDef hi2c1;

UART_HandleTypeDef huart2;

/* USER CODE BEGIN PV */
/* Latest IMU sample. File-scope (not locals in main) so the debugger's Live
   Expressions can resolve them while the target is running - a stack local has
   no fixed address to read live. volatile keeps each new sample from being
   optimized away before the JSON telemetry chunk consumes it. */
volatile int16_t ax, ay, az, gx, gy, gz;      /* raw signed 16-bit counts */
volatile float   ax_g, ay_g, az_g;            /* acceleration, g */
volatile float   gx_dps, gy_dps, gz_dps;      /* angular rate, deg/s */
volatile int16_t gx_off, gy_off, gz_off;      /* gyro zero-rate bias, raw counts */
volatile uint32_t seq;                        /* monotonic telemetry sequence number */

/* Latest BME280 environmental sample, in engineering units. File-scope for the
   same Live Expressions reason as the IMU globals above. The driver's per-chip
   calibration lives in bme280 (filled once at init), not here. */
BME280_t bme280;
volatile float temp_c;      /* degrees Celsius */
volatile float pressure_hpa;/* hectopascals (== mbar) */
volatile float humidity_pct;/* %RH */
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART2_UART_Init(void);
static void MX_I2C1_Init(void);
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
#include <stdio.h>   /* snprintf */

/* Recover a wedged I2C bus. After a WARM reset (debugger restart / reset button)
   the STM32 restarts but the sensors do not; one left mid-transfer can keep
   holding SDA low, jamming the bus so every HAL read times out (this is the
   startup hang we hit). The standard fix: drive SCL manually as a GPIO and clock
   out up to 9 pulses until the slave releases SDA, issue a STOP, then re-init the
   peripheral. PB8 = SCL, PB9 = SDA (see HAL_I2C_MspInit). */
static void I2C1_BusRecover(void)
{
  GPIO_InitTypeDef gpio = {0};

  HAL_I2C_DeInit(&hi2c1);            /* hand PB8/PB9 back from the I2C peripheral */
  __HAL_RCC_GPIOB_CLK_ENABLE();

  gpio.Mode  = GPIO_MODE_OUTPUT_OD;  /* open-drain, like a real I2C line */
  gpio.Pull  = GPIO_NOPULL;          /* the sensor modules carry the pull-ups */
  gpio.Speed = GPIO_SPEED_FREQ_LOW;
  gpio.Pin   = GPIO_PIN_8 | GPIO_PIN_9;
  HAL_GPIO_Init(GPIOB, &gpio);
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8 | GPIO_PIN_9, GPIO_PIN_SET); /* idle high */

  /* Pulse SCL until SDA is released (max 9 = one byte + ack). */
  for (int i = 0; i < 9 && HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_9) == GPIO_PIN_RESET; i++)
  {
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_RESET);
    HAL_Delay(1);
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_SET);
    HAL_Delay(1);
  }

  /* Generate a STOP: SDA rises while SCL is high. */
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_9, GPIO_PIN_RESET);
  HAL_Delay(1);
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_SET);
  HAL_Delay(1);
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_9, GPIO_PIN_SET);
  HAL_Delay(1);

  MX_I2C1_Init();                    /* re-init peripheral; restores PB8/PB9 to AF */
}

/* Format a float as a fixed-decimal string WITHOUT floating-point printf.
   newlib-nano omits %f by default (and pulling it in bloats a constrained MCU),
   so we split the value into an integer part and a zero-padded fractional part
   and print those with plain %ld. Returns bytes written (snprintf semantics). */
static int fmt_fixed(char *buf, size_t n, float v, int decimals)
{
  int neg = (v < 0.0f);
  if (neg) { v = -v; }
  int32_t mul = 1;
  for (int i = 0; i < decimals; i++) { mul *= 10; }
  int32_t scaled = (int32_t)(v * (float)mul + 0.5f);   /* round to N decimals */
  int32_t whole  = scaled / mul;
  int32_t frac   = scaled % mul;
  return snprintf(buf, n, "%s%ld.%0*ld", neg ? "-" : "", (long)whole, decimals, (long)frac);
}

/* Frame the latest sample as ONE newline-delimited JSON object and push it over
   UART. Newline-delimited JSON (NDJSON) = one complete JSON value per line, '\n'
   as the record separator - so the gateway can split the stream on newlines and
   know each line is a whole message. */
static void Telemetry_Emit(void)
{
  char line[TELEMETRY_LINE_MAX];
  char st[12], sh[12], sp[12];                 /* temp, humidity, pressure */
  char sax[12], say[12], saz[12];              /* accel, g */
  char sgx[12], sgy[12], sgz[12];              /* gyro, dps */

  fmt_fixed(st,  sizeof st,  temp_c,       2);
  fmt_fixed(sh,  sizeof sh,  humidity_pct, 2);
  fmt_fixed(sp,  sizeof sp,  pressure_hpa, 2);
  fmt_fixed(sax, sizeof sax, ax_g, 3);
  fmt_fixed(say, sizeof say, ay_g, 3);
  fmt_fixed(saz, sizeof saz, az_g, 3);
  fmt_fixed(sgx, sizeof sgx, gx_dps, 2);
  fmt_fixed(sgy, sizeof sgy, gy_dps, 2);
  fmt_fixed(sgz, sizeof sgz, gz_dps, 2);

  int len = snprintf(line, sizeof line,
      "{\"id\":\"%s\",\"type\":\"telemetry\",\"seq\":%lu,\"ts\":%lu,"
      "\"temp\":%s,\"humidity\":%s,\"pressure\":%s,"
      "\"ax\":%s,\"ay\":%s,\"az\":%s,"
      "\"gx\":%s,\"gy\":%s,\"gz\":%s}\r\n",
      DEVICE_ID, (unsigned long)seq, (unsigned long)HAL_GetTick(),
      st, sh, sp, sax, say, saz, sgx, sgy, sgz);

  if (len > 0)
  {
    HAL_UART_Transmit(&huart2, (uint8_t *)line, (uint16_t)len, TELEMETRY_UART_TIMEOUT);
    seq++;   /* advance only after a line is framed (monotonic per message) */
  }
}

/* Emit a heartbeat line: a payload-free "I'm alive" message on its own fixed
   cadence. It shares the monotonic seq with telemetry (so any gap in the merged
   stream = a lost line) and carries the uptime (ts). Its job is liveness that is
   independent of sensor data - if telemetry stalls but heartbeats keep coming,
   the device and link are fine and the fault is upstream in the sensor path. */
static void Heartbeat_Emit(void)
{
  char line[TELEMETRY_LINE_MAX];
  int len = snprintf(line, sizeof line,
      "{\"id\":\"%s\",\"type\":\"heartbeat\",\"seq\":%lu,\"ts\":%lu}\r\n",
      DEVICE_ID, (unsigned long)seq, (unsigned long)HAL_GetTick());
  if (len > 0)
  {
    HAL_UART_Transmit(&huart2, (uint8_t *)line, (uint16_t)len, TELEMETRY_UART_TIMEOUT);
    seq++;
  }
}
/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */
  uint8_t who_am_i = 0;              /* raw WHO_AM_I byte read back from the MPU */
  HAL_StatusTypeDef whoami_status;   /* result of the I2C read */
  uint8_t bme_id = 0;                /* raw chip-ID byte read back from the BME280 */
  HAL_StatusTypeDef bme_id_status;   /* result of the I2C read */
  uint8_t mpu_wake = 0x00;           /* value written to PWR_MGMT_1 to clear SLEEP */
  uint8_t mpu_dlpf;                  /* DLPF config byte written to CONFIG/ACCEL_CONFIG2 */
  int32_t gx_sum = 0, gy_sum = 0, gz_sum = 0; /* gyro-bias calibration accumulators */
  uint16_t gx_n = 0, gy_n = 0, gz_n = 0;      /* count of accepted (non-railed) samples */
  uint8_t imu_raw[MPU6500_BURST_LEN];/* one atomic burst: accel, temp, gyro */
  HAL_StatusTypeDef imu_status;      /* result of each sampling read */
  uint32_t last_sample = 0;          /* SysTick ms of the last sample tick */
  uint32_t last_led = 0;             /* SysTick ms of the last LED toggle */
  uint32_t last_hb = 0;              /* SysTick ms of the last heartbeat line */
  /* ax..gz and the g/dps outputs are file-scope globals (see PV) so Live
     Expressions can watch them while the target runs. */
  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_USART2_UART_Init();
  MX_I2C1_Init();
  /* USER CODE BEGIN 2 */
  /* Clear any bus lockup left by a warm reset before the first transaction. */
  I2C1_BusRecover();

  /* Read the MPU-6500 WHO_AM_I register as an I2C sanity check (expect 0x70). */
  whoami_status = HAL_I2C_Mem_Read(&hi2c1,
                                   MPU6500_I2C_ADDR,
                                   MPU6500_REG_WHOAMI,
                                   I2C_MEMADD_SIZE_8BIT,
                                   &who_am_i,
                                   1,
                                   MPU6500_I2C_TIMEOUT);
  if (whoami_status != HAL_OK || who_am_i != MPU6500_WHOAMI_ID)
  {
    Error_Handler();
  }

  /* Read the BME280 chip-ID register as an I2C sanity check (expect 0x60). */
  bme_id_status = HAL_I2C_Mem_Read(&hi2c1,
                                   BME280_I2C_ADDR,
                                   BME280_REG_ID,
                                   I2C_MEMADD_SIZE_8BIT,
                                   &bme_id,
                                   1,
                                   BME280_I2C_TIMEOUT);
  if (bme_id_status != HAL_OK || bme_id != BME280_CHIP_ID)
  {
    Error_Handler();
  }

  /* Wake the MPU-6500: on power-up SLEEP is set and the data registers never
     update. Writing 0x00 to PWR_MGMT_1 clears SLEEP and selects the internal
     clock. Must succeed, so fail closed like the ID checks. */
  if (HAL_I2C_Mem_Write(&hi2c1,
                        MPU6500_I2C_ADDR,
                        MPU6500_REG_PWR_MGMT_1,
                        I2C_MEMADD_SIZE_8BIT,
                        &mpu_wake,
                        1,
                        MPU6500_I2C_TIMEOUT) != HAL_OK)
  {
    Error_Handler();
  }

  /* Set the gyro/temp and accel Digital Low-Pass Filters. Without this the gyro
     runs wide-open (~250 Hz bandwidth) and dumps broadband noise into every
     sample; a ~41/44 Hz cutoff keeps the real (slow) motion and drops the
     high-frequency jitter. Fail closed like the ID checks. */
  mpu_dlpf = MPU6500_DLPF_CFG_41HZ;
  if (HAL_I2C_Mem_Write(&hi2c1, MPU6500_I2C_ADDR, MPU6500_REG_CONFIG,
                        I2C_MEMADD_SIZE_8BIT, &mpu_dlpf, 1,
                        MPU6500_I2C_TIMEOUT) != HAL_OK)
  {
    Error_Handler();
  }
  mpu_dlpf = MPU6500_ADLPF_CFG_44HZ;
  if (HAL_I2C_Mem_Write(&hi2c1, MPU6500_I2C_ADDR, MPU6500_REG_ACCEL_CONFIG2,
                        I2C_MEMADD_SIZE_8BIT, &mpu_dlpf, 1,
                        MPU6500_I2C_TIMEOUT) != HAL_OK)
  {
    Error_Handler();
  }

  /* Gyro zero-rate calibration. A MEMS gyro reads a nonzero constant even when
     perfectly still (the bias we saw as gx ~ 6.77 dps). With the board held
     still at boot, average a batch of samples per axis - that mean IS the bias -
     then subtract it from every later reading. Accel is left alone: gravity is
     its built-in reference.
     Two requirements for a clean offset: (1) the board must be MOTIONLESS for
     the whole window - any movement leaks into the mean; (2) let the gyro settle
     first, since it has a start-up transient right after wake/DLPF-config that
     would otherwise skew the early samples. */
  HAL_Delay(100);  /* let the gyro clear its power-on transient before sampling.
                      Note this does NOT remove the ~0.7 dps residual on gx: that's
                      thermal bias drift (we calibrate cold, the die then warms and
                      the bias creeps), which a one-shot boot calibration can't
                      track. Sub-1 dps at rest is a healthy stationary gyro. */
  for (uint16_t i = 0; i < MPU6500_GYRO_CAL_SAMPLES; i++)
  {
    if (HAL_I2C_Mem_Read(&hi2c1, MPU6500_I2C_ADDR, MPU6500_REG_ACCEL_XOUT_H,
                         I2C_MEMADD_SIZE_8BIT, imu_raw, MPU6500_BURST_LEN,
                         MPU6500_I2C_TIMEOUT) == HAL_OK)
    {
      int16_t rgx = (int16_t)((imu_raw[8]  << 8) | imu_raw[9]);
      int16_t rgy = (int16_t)((imu_raw[10] << 8) | imu_raw[11]);
      int16_t rgz = (int16_t)((imu_raw[12] << 8) | imu_raw[13]);
      /* Reject railed/outlier samples per axis BEFORE averaging: a real at-rest
         reading is well under the bias bound, so anything past it is a start-up
         transient or a bump, not bias. This is what let gy railing poison the
         old whole-window average and get the axis zeroed - now we just drop
         those samples and keep gy's true ~1 dps offset. */
      if (rgx < MPU6500_GYRO_BIAS_MAX_CNT && rgx > -MPU6500_GYRO_BIAS_MAX_CNT) { gx_sum += rgx; gx_n++; }
      if (rgy < MPU6500_GYRO_BIAS_MAX_CNT && rgy > -MPU6500_GYRO_BIAS_MAX_CNT) { gy_sum += rgy; gy_n++; }
      if (rgz < MPU6500_GYRO_BIAS_MAX_CNT && rgz > -MPU6500_GYRO_BIAS_MAX_CNT) { gz_sum += rgz; gz_n++; }
    }
    HAL_Delay(2);
  }
  /* Average only the accepted samples. If an axis had none (railed the whole
     window), fall back to no correction - a bad calibration is worse than none. */
  gx_off = gx_n ? (int16_t)(gx_sum / gx_n) : 0;
  gy_off = gy_n ? (int16_t)(gy_sum / gy_n) : 0;
  gz_off = gz_n ? (int16_t)(gz_sum / gz_n) : 0;

  /* Initialize the BME280 once: the driver reads the chip's factory NVM
     calibration and configures oversampling x1 + normal (continuous) mode.
     Unlike the MPU, we can't just scale raw counts - every BME is individually
     trimmed, so this calibration read is mandatory before any reading is valid.
     Fail closed like the ID checks. */
  if (BME280_Init(&bme280, &hi2c1, BME280_I2C_ADDR) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    /* Non-blocking scheduler: instead of a HAL_Delay spin (which parks the CPU
       and makes timing drift with how long the body takes), we poll the SysTick
       millisecond counter (HAL_GetTick) and fire each task when its period is
       due. The (uint32_t) subtraction is wrap-around safe. */
    uint32_t now = HAL_GetTick();

    /* Heartbeat LED - a separate cadence so a stalled sensor read never freezes
       the "I'm alive" signal. */
    if ((uint32_t)(now - last_led) >= HEARTBEAT_LED_MS)
    {
      last_led += HEARTBEAT_LED_MS;
      HAL_GPIO_TogglePin(LD2_GPIO_Port, LD2_Pin);
    }

    /* Heartbeat line - its own cadence, independent of sampling, so it keeps
       proving liveness even if a sensor read stalls the telemetry path. */
    if ((uint32_t)(now - last_hb) >= HEARTBEAT_MSG_MS)
    {
      last_hb += HEARTBEAT_MSG_MS;
      Heartbeat_Emit();
    }

    /* Fixed-rate sampling at SAMPLE_PERIOD_MS (10 Hz). Advancing last_sample by
       exactly one period (not "= now") keeps the average rate locked to the
       tick, so it doesn't drift with body execution time. */
    if ((uint32_t)(now - last_sample) >= SAMPLE_PERIOD_MS)
    {
      last_sample += SAMPLE_PERIOD_MS;

      /* Sample the IMU: one 14-byte burst from ACCEL_XOUT_H (the MPU auto-
         increments its register pointer), split each high/low pair into a
         signed count, subtract the gyro bias, and scale to engineering units. */
      imu_status = HAL_I2C_Mem_Read(&hi2c1,
                                    MPU6500_I2C_ADDR,
                                    MPU6500_REG_ACCEL_XOUT_H,
                                    I2C_MEMADD_SIZE_8BIT,
                                    imu_raw,
                                    MPU6500_BURST_LEN,
                                    MPU6500_I2C_TIMEOUT);
      if (imu_status == HAL_OK)
      {
        ax = (int16_t)((imu_raw[0]  << 8) | imu_raw[1]);
        ay = (int16_t)((imu_raw[2]  << 8) | imu_raw[3]);
        az = (int16_t)((imu_raw[4]  << 8) | imu_raw[5]);
        /* imu_raw[6..7] = on-die temperature, unused (telemetry temp is BME280's). */
        gx = (int16_t)(((int16_t)((imu_raw[8]  << 8) | imu_raw[9]))  - gx_off);
        gy = (int16_t)(((int16_t)((imu_raw[10] << 8) | imu_raw[11])) - gy_off);
        gz = (int16_t)(((int16_t)((imu_raw[12] << 8) | imu_raw[13])) - gz_off);

        ax_g = ax / MPU6500_ACCEL_SENS;
        ay_g = ay / MPU6500_ACCEL_SENS;
        az_g = az / MPU6500_ACCEL_SENS;
        gx_dps = gx / MPU6500_GYRO_SENS;
        gy_dps = gy / MPU6500_GYRO_SENS;
        gz_dps = gz / MPU6500_GYRO_SENS;
      }
      /* A dropped sample leaves the previous values in place for now; proper
         error handling / degraded state lands in a later chunk. */

      /* Sample the BME280: the driver burst-reads the raw registers and runs the
         per-chip compensation, handing back real units. */
      float t, p, h;
      if (BME280_Read(&bme280, &t, &p, &h) == HAL_OK)
      {
        temp_c       = t;
        pressure_hpa = p;
        humidity_pct = h;
      }

      /* Frame this sample as one NDJSON line and stream it over UART. */
      Telemetry_Emit();
    }
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE2);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSI;
  RCC_OscInitStruct.PLL.PLLM = 16;
  RCC_OscInitStruct.PLL.PLLN = 336;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV4;
  RCC_OscInitStruct.PLL.PLLQ = 7;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief I2C1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_I2C1_Init(void)
{

  /* USER CODE BEGIN I2C1_Init 0 */

  /* USER CODE END I2C1_Init 0 */

  /* USER CODE BEGIN I2C1_Init 1 */

  /* USER CODE END I2C1_Init 1 */
  hi2c1.Instance = I2C1;
  hi2c1.Init.ClockSpeed = 100000;
  hi2c1.Init.DutyCycle = I2C_DUTYCYCLE_2;
  hi2c1.Init.OwnAddress1 = 0;
  hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  hi2c1.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  hi2c1.Init.OwnAddress2 = 0;
  hi2c1.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  hi2c1.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&hi2c1) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN I2C1_Init 2 */

  /* USER CODE END I2C1_Init 2 */

}

/**
  * @brief USART2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART2_UART_Init(void)
{

  /* USER CODE BEGIN USART2_Init 0 */

  /* USER CODE END USART2_Init 0 */

  /* USER CODE BEGIN USART2_Init 1 */

  /* USER CODE END USART2_Init 1 */
  huart2.Instance = USART2;
  huart2.Init.BaudRate = 115200;
  huart2.Init.WordLength = UART_WORDLENGTH_8B;
  huart2.Init.StopBits = UART_STOPBITS_1;
  huart2.Init.Parity = UART_PARITY_NONE;
  huart2.Init.Mode = UART_MODE_TX_RX;
  huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart2.Init.OverSampling = UART_OVERSAMPLING_16;
  if (HAL_UART_Init(&huart2) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART2_Init 2 */

  /* USER CODE END USART2_Init 2 */

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOH_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(LD2_GPIO_Port, LD2_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin : B1_Pin */
  GPIO_InitStruct.Pin = B1_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_IT_FALLING;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(B1_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pin : LD2_Pin */
  GPIO_InitStruct.Pin = LD2_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(LD2_GPIO_Port, &GPIO_InitStruct);

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
