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

/* BME280 chip-ID sanity check: genuine BME280 reports 0x60 in its id register.
   A BMP280 reports 0x58 and has NO humidity sensor, so it would silently break
   the humidity telemetry field - verify the part before trusting it. */
#define BME280_I2C_ADDR     (0x76 << 1)  /* 7-bit 0x76, HAL wants it << 1 */
#define BME280_REG_ID       0xD0U
#define BME280_CHIP_ID      0x60U        /* BME280 (a BMP280 reads 0x58) */
#define BME280_I2C_TIMEOUT  100U         /* ms */
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
  uint8_t imu_raw[MPU6500_BURST_LEN];/* one atomic burst: accel, temp, gyro */
  HAL_StatusTypeDef imu_status;      /* result of each sampling read */
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
    HAL_GPIO_TogglePin(LD2_GPIO_Port, LD2_Pin);
    HAL_Delay(500);
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    /* Sample the IMU: one 14-byte burst from ACCEL_XOUT_H (the MPU auto-
       increments its register pointer), then split each high/low pair into a
       signed count and scale to engineering units. */
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
      /* imu_raw[6..7] = on-die temperature, unused (telemetry temp is BME280's) */
      gx = (int16_t)((imu_raw[8]  << 8) | imu_raw[9]);
      gy = (int16_t)((imu_raw[10] << 8) | imu_raw[11]);
      gz = (int16_t)((imu_raw[12] << 8) | imu_raw[13]);

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
       per-chip compensation, handing back real units. Read into locals, then
       publish to the volatile globals the telemetry chunk will consume. */
    float t, p, h;
    if (BME280_Read(&bme280, &t, &p, &h) == HAL_OK)
    {
      temp_c       = t;
      pressure_hpa = p;
      humidity_pct = h;
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
